"""`fa orch`: diagnose and recover blocked issues; the local request and question box."""

from foragent.commands import facade, repo_store

ORDER = 120
# An orchestrator whose heartbeat is at most this old counts as alive.
ALIVE_SECONDS = 30
INBOX_ACTIONS = {"request", "inbox", "request-status", "ask", "questions", "answer", "note", "queue", "alive",
                 "reviewers", "ensure", "facts", "scorecard"}


def add(commands) -> None:
    cli = facade()
    orchestrator = commands.add_parser("orch", help="Diagnose and recover blocked issues")
    orch_actions = orchestrator.add_subparsers(dest="action", required=True)
    for name in ("list", "rerun-review", "retry", "resolve", "escalate", "log", "watch", "premerge", "merge"):
        command = orch_actions.add_parser(name)
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        if name == "list":
            command.add_argument("--json", action="store_true")
        elif name == "watch":
            command.add_argument("--interval", type=float, default=5, help="Poll interval in seconds (default: 5)")
            command.add_argument("--follow", action="store_true", help="Keep printing changes until interrupted")
        elif name != "log":
            command.add_argument("number", type=int)
        if name in ("retry", "rerun-review", "resolve", "escalate"):
            command.add_argument("--say", help="One line for a person, shown in the view (docs/voice.md voice)")
        if name == "premerge":
            command.add_argument("--json", action="store_true")
        elif name == "merge":
            command.add_argument("--approved-by", required=True)
            command.add_argument("--at")
        elif name == "retry":
            command.add_argument("--hint")
        elif name == "resolve":
            command.add_argument("--verdict", choices=("pass", "fail"), required=True)
            command.add_argument("--note", required=True)
        elif name == "escalate":
            command.add_argument("--summary", required=True)
            command.add_argument("--tried", required=True)
            command.add_argument("--option", action="append", default=[])
            command.add_argument("--recommend", type=int)
    from foragent.commands._orch_architect import add_orch
    from foragent.commands._orch_box import _add_inbox

    _add_inbox(orch_actions)
    add_orch(orch_actions)


def __getattr__(name):
    """The box handlers live in _orch_box; `from foragent.commands.orch import _orch_inbox` keeps working."""
    if name in ("_add_inbox", "_alive", "_orch_inbox"):
        from foragent.commands import _orch_box

        return getattr(_orch_box, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _list(ctx, args) -> None:
    cli = facade()
    items = facade().orch.list_blocked(ctx)
    if args.json:
        print(cli.json.dumps(items, ensure_ascii=False))
    elif not items:
        print("No blocked issues.")
    else:
        for item in items:
            print(f"#{item['number']} {item['title']} [{item['lane']}] "
                  f"{item['reason'] or 'unknown'} · {item['blocked_at'] or 'unknown time'}")
            print(item["details"])
            for review in item["reviews"]:
                print(f"Review: {review['path']}\n{review['text']}")
            print(f"Log (last 60 lines): {item['log_path']}\n{item['log_tail']}")


def run(args) -> int:
    if handler := getattr(args, "handler", None):
        return handler(args)
    cli = facade()
    root = args.repo_root.resolve()
    if args.action in cli.INBOX_ACTIONS:
        config, repo, store = repo_store(root)
        cli._orch_inbox(args, store, config, repo)
        return 0
    git = cli.GitRepo(root)
    config = cli.load_config(root)
    repo = config.repo or git.remote_slug()
    tracker = cli.GhTracker(repo)
    store = cli.StateStore.for_repo(repo)
    ctx = cli.RunContext(repo=repo, repo_root=root, tracker=tracker, store=store,
                         config=config, git=git, now=lambda: cli.datetime.now(cli.UTC))
    orch = cli.orch
    if args.action == "list":
        _list(ctx, args)
    elif args.action == "log":
        for event in orch.log(ctx):
            print(f"{event['at']} [{event['kind']}] {event['summary']}")
    elif args.action == "watch":
        cli._open_lane_view(root)
        unknown = False
        try:
            for snapshot in orch.watch(ctx, interval=args.interval, follow=args.follow):
                print(cli.json.dumps(snapshot, ensure_ascii=False), flush=True)
                unknown = bool(snapshot.get("unknown"))
        except KeyboardInterrupt:
            pass
        if unknown and not args.follow:
            return 1
    elif args.action == "retry":
        print(orch.retry(ctx, args.number, args.hint, say=args.say))
    elif args.action == "rerun-review":
        print(orch.rerun_review(ctx, args.number, say=args.say))
    elif args.action == "resolve":
        print(orch.resolve(ctx, args.number, args.verdict, args.note, say=args.say))
    elif args.action == "escalate":
        print(orch.escalate(ctx, args.number, args.summary, args.tried, args.option, args.recommend,
                            say=args.say))
    elif args.action == "premerge":
        report = orch.premerge(ctx, args.number)
        if args.json:
            print(cli.json.dumps(report, ensure_ascii=False))
        else:
            print(f"PR #{report['pr']}: tests {report['tests']}; "
                  f"conflicts: {', '.join(report['conflicts']) or 'none'}; "
                  f"CI {report['ci']}; ready: {str(report['ready']).lower()}")
        return 0 if report["ready"] else 1
    elif args.action == "merge":
        print(orch.merge(ctx, args.number, approved_by=args.approved_by, at=args.at))
    return 0

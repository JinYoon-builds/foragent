"""`fa orch`: diagnose and recover blocked issues; the local request and question box."""

from foragent.commands import facade, repo_store

ORDER = 120
# An orchestrator whose heartbeat is at most this old counts as alive.
ALIVE_SECONDS = 30
INBOX_ACTIONS = {"request", "inbox", "request-status", "ask", "questions", "answer", "note", "queue", "alive",
                 "reviewers", "facts"}


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
    _add_inbox(orch_actions)


def _add_inbox(orch_actions) -> None:
    cli = facade()
    request = orch_actions.add_parser("request", help="Put a planning request in the durable box")
    request.add_argument("--from", dest="source", required=True, help="Sending session, e.g. planning")
    request.add_argument("--text", required=True)
    inbox = orch_actions.add_parser("inbox", help="List requests in the box (read this first on start)")
    inbox.add_argument("--pending", action="store_true", help="Hide requests marked done")
    request_status = orch_actions.add_parser("request-status", help="Mark a request processing or done")
    request_status.add_argument("id")
    request_status.add_argument("status", choices=cli.orch_inbox.REQUEST_STATUSES)
    request_status.add_argument("--note")
    ask = orch_actions.add_parser("ask", help="Record a D71 human question in the box")
    ask.add_argument("--question", required=True)
    ask.add_argument("--option", action="append", default=[])
    ask.add_argument("--recommend", type=int)
    questions = orch_actions.add_parser("questions", help="List open human questions")
    questions.add_argument("--all", action="store_true", help="Include answered questions")
    answer = orch_actions.add_parser("answer", help="Record the answer and close one question")
    answer.add_argument("id")
    answer.add_argument("--text", required=True)
    note = orch_actions.add_parser("note", help="Record what the orchestrator does now, why, next and where")
    note.add_argument("--now", required=True)
    note.add_argument("--why")
    note.add_argument("--next", dest="next_step")
    note.add_argument("--where-title", "--orca-title", dest="where_title", help="Orca tab title, e.g. orch: <project>")
    note.add_argument("--tmux-pane", help="tmux pane id when not taken from TMUX_PANE")
    for command in (request, inbox, request_status, ask, questions, answer, note):
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        if command in (inbox, questions):
            command.add_argument("--json", action="store_true")
    alive = orch_actions.add_parser("alive", help=cli.text("heartbeats_help"))
    alive.add_argument("--json", action="store_true")
    alive.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    queue = orch_actions.add_parser("queue", help="Set, show or clear a lane's or the cross-lane start order")
    queue.add_argument("lane", nargs="?", help="Lane name; with --global, the first issue number")
    queue.add_argument("--global", dest="global_", action="store_true",
                       help="The cross-lane priority list instead of one lane's queue")
    queue.add_argument("numbers", type=int, nargs="*", help="Issue numbers in start order; none shows the queue")
    queue.add_argument("--clear", action="store_true")
    queue.add_argument("--json", action="store_true")
    queue.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    facts = orch_actions.add_parser(
        "facts", help="Show measured run, test-slot, file, failure and resource facts from local records")
    facts.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    facts.add_argument("--json", action="store_true")
    reviewers = orch_actions.add_parser("reviewers", help="Set or clear how many reviewers check one issue's risk-map PRs")
    reviewers.add_argument("issue", type=int)
    reviewers.add_argument("count", type=int, nargs="?", help="Reviewer count (1 or more); wins over review_reviewers")
    reviewers.add_argument("--clear", action="store_true")
    reviewers.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def _reviewers(args, store, config) -> None:
    """Per-issue reviewer count in the state directory; .foragent.yml is never written."""
    from foragent import orch_reviewers
    from foragent.i18n import text

    language = config.language
    if args.clear == (args.count is not None):
        raise SystemExit(text("orch_reviewers_usage", language))
    if args.clear:
        orch_reviewers.clear(store, args.issue)
        print(text("orch_reviewers_cleared", language, issue=args.issue,
                   count=orch_reviewers.effective(store, config, args.issue)))
        return
    if args.count < 1:
        raise SystemExit(text("orch_reviewers_invalid", language, count=args.count,
                              current=orch_reviewers.effective(store, config, args.issue)))
    orch_reviewers.set_count(store, args.issue, args.count)
    print(text("orch_reviewers_set", language, issue=args.issue, count=args.count))


def _global_queue(args, store, config) -> None:
    """Cross-lane priority list in the state directory; argparse puts the first number in ``lane``."""
    cli = facade()
    text, language = cli.text, config.language
    numbers = list(args.numbers)
    if args.lane is not None:
        try:
            numbers.insert(0, int(args.lane))
        except ValueError:
            raise SystemExit(text("orch_queue_global_number", language, value=args.lane)) from None
    if args.clear and numbers:
        raise SystemExit(text("orch_queue_global_usage", language))
    order = cli.orch_queue.set_global(store, []) if args.clear else (
        cli.orch_queue.set_global(store, numbers) if numbers else cli.orch_queue.get_global(store))
    if args.json:
        print(cli.json.dumps({"lane": None, "order": order}))
    elif order:
        print(text("orch_queue_global_show", language, order=" ".join(f"#{n}" for n in order)))
    else:
        print(text("orch_queue_global_empty", language))


def _orch_inbox(args, store, config, repo: str) -> None:
    """Local request/question box; never reads GitHub or starts an executor."""
    cli = facade()
    box = cli.orch_inbox
    if args.action == "reviewers":
        _reviewers(args, store, config)
        return
    if args.action == "alive":
        _alive(args, store, config)
        return
    if args.action == "facts":
        # The checkout config; collect applies the tick's recorded committed config from its frozen read.
        facts = cli.orch_facts.collect(store, config, args.repo_root.resolve(), checkout=True)
        print(cli.json.dumps(facts, ensure_ascii=False) if args.json else cli.orch_facts.human(facts))
        return
    if args.action == "queue":
        if args.global_:
            _global_queue(args, store, config)
            return
        if args.lane is None:
            raise SystemExit(cli.text("orch_queue_lane_required", config.language))
        if args.clear and args.numbers:
            raise SystemExit("fa orch queue: give issue numbers or --clear, not both")
        order = cli.orch_queue.set_order(store, args.lane, []) if args.clear else (
            cli.orch_queue.set_order(store, args.lane, args.numbers) if args.numbers
            else cli.orch_queue.get(store, args.lane))
        if args.json:
            print(cli.json.dumps({"lane": args.lane, "order": order}))
        elif order:
            print(f"lane:{args.lane} queue: " + " ".join(f"#{n}" for n in order))
        else:
            print(f"lane:{args.lane} queue is empty; issues start by number.")
        return
    if args.action == "request":
        record = box.add_request(store, args.source, args.text)
        print(f"Request {record['id']} saved ({record['status']}).")
    elif args.action == "request-status":
        record = box.set_request_status(store, args.id, args.status, args.note)
        print(f"Request {record['id']}: {record['status']}.")
    elif args.action == "note":
        where = box.note_location(dict(cli.os.environ), args.where_title, args.tmux_pane)
        record = box.write_note(store, args.now, args.why, args.next_step, where)
        print(f"Note saved at {record['at']} ({where['kind']}).")
    elif args.action == "ask":
        record = box.ask(store, args.question, args.option, args.recommend)
        pushed = box.route_question(store, config, repo, record)
        print(f"Question {record['id']} saved (open)"
              + ("; no planning session is alive, so a notification was sent." if pushed else "."))
    elif args.action == "answer":
        record = box.answer(store, args.id, args.text)
        print(f"Question {record['id']} closed.")
    elif args.action == "inbox":
        rows = box.requests(store, pending=args.pending)
        if args.json:
            print(cli.json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No requests.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] from {row['from']} · {row['created_at']}")
            print(row["text"])
            for note in row.get("notes", []):
                print(f"  {note['at']} {note['status']}: {note['note']}")
    else:
        rows = box.questions(store, include_closed=args.all)
        if args.json:
            print(cli.json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No open questions.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] {row['question']}")
            for index, option in enumerate(row["options"], 1):
                mark = " (recommended)" if index == row["recommend"] else ""
                print(f"  {index}. {option}{mark}")
            if row.get("answer"):
                print(f"  answer: {row['answer']}")


def _alive(args, store, config) -> None:
    cli = facade()
    last = store.heartbeats()["orchestrator"]
    elapsed = None if last is None else (store._now() - last).total_seconds()
    # Compare the exact elapsed time; the whole seconds below are only for display.
    alive = elapsed is not None and elapsed <= ALIVE_SECONDS
    age = None if elapsed is None else int(elapsed)
    if args.json:
        print(cli.json.dumps({"alive": alive, "last_seen": last and last.isoformat(), "age_seconds": age}))
    elif last is None:
        print(cli.text("heartbeats_never", config.language))
    else:
        key = "heartbeats_alive" if alive else "heartbeats_gone"
        print(cli.text(key, config.language, age=age, at=last.isoformat()))


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

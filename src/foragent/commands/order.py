"""`fa order`: deterministic tools for fa-order."""

from foragent.commands import facade, repo_store

ORDER = 80

PUBLISH_EPILOG = '''PLAN.json schema (unknown fields are rejected):
{
  "id": "2026-10-20-login", "title": "Login MVP", "summary": "Goal of this order",
  "spec_markdown": "# Login\\n...",
  "new_lanes": [{"name": "api", "paths": ["api/**"], "skills": []}],
  "issues": [{"key": "login", "title": "Login API", "goal": "...", "out_of_scope": "...",
    "done_when": ["uv run pytest tests/acceptance/test_login.py"], "checklist": [],
    "lane": "api", "size": "m", "interactive": false, "blocked_by": [], "files": ["src/api/**"],
    "acceptance_files": {"tests/acceptance/test_login.py": "..."},
    "references": ["docs/design/login/HANDOFF.md"]}],
  "edits": [{"number": 3, "goal": "...", "out_of_scope": "...", "done_when": ["true"],
    "checklist": [], "blocked_by": ["login"], "files": [], "acceptance_files": {}, "references": []}],
  "reference_files": {"docs/design/login/HANDOFF.md": "# Approved login screen\n...",
    "docs/design/login/approved.png": {"path": "docs/design/login/approved.png",
      "base64": "<encoded image bytes>"}}
}
new_lanes, edits, checklist, blocked_by, files, and acceptance_files default to empty collections.
edits[].blocked_by names this plan's issue keys and replaces the edited issue's native
dependencies ([] clears them); omit it to keep them.
A "## Waiting" section in a goal is the issue's wait (reason, next condition, responsible role);
publication comments one line when it changes and nothing when it is unchanged.
issues[].replaces optionally lists unique positive issue numbers (default []); approved replacements
close them as not planned with a comment linking their successors. --dry-run previews closures.
issues[].interactive is an optional boolean (default false); true adds the interactive label.
decisions (issues and edits) optionally lists ledger IDs such as ["D79"]; publication writes one
decision marker. Omitted keeps an issue's existing marker; [] clears its links.
issues[].statements and edits[].statements are optional nonempty lists of one-line, plain-language
behaviour statements, shown under "## Behaviour". Their presence selects the reviewed-tests route
for good: a real publish (with or without approval) refuses the issue until its lane has written
tests and the configured reviewer has reviewed them; --dry-run still previews it.
--approve (or --approve-keys) needs a clean `fa order preread` of each approved issue's current
content; --dry-run never needs one.
files lists repository-relative paths or globs expected to change; absolute paths and .. are rejected.
reference_files carries approved design artifacts, published in the same commit as the spec and
tests. Keys are normalized repository paths under docs/design/ (no absolute paths, .., symlinks).
Text files are strings; png, jpg (jpeg) and gif images are objects with exactly "path" (equal to
the key) and "base64" (strict base64). Each file may be at most 1048576 bytes (1 MiB), counted as
UTF-8 text bytes or decoded image bytes. "references" on an issue or edit selects reference files
for its ## References links and preread; omit it for all reference files, [] selects none.'''


def _comma_keys(value: str) -> list[str]:
    cli = facade()
    values = value.split(",")
    if any(not item.strip() for item in values):
        raise cli.argparse.ArgumentTypeError("expected comma-separated keys")
    return [item.strip() for item in values]


def _comma_numbers(value: str) -> list[int]:
    cli = facade()
    try:
        numbers = [int(item) for item in facade()._comma_keys(value)]
        if any(number <= 0 for number in numbers):
            raise ValueError
        return numbers
    except ValueError as exc:
        raise cli.argparse.ArgumentTypeError("expected comma-separated positive issue numbers") from exc


def _help_language() -> str:
    """The current repository's configured language for help text, else English."""
    from foragent.config import _read_yaml

    # Only the repository file's language: building the parser must not load the full config.
    try:
        language = _read_yaml(facade().Path.cwd() / ".foragent.yml").get("language")
    except Exception:  # Help must render even where no valid configuration exists.
        return "en"
    return language if isinstance(language, str) else "en"


def add(commands) -> None:
    cli = facade()
    order_command = commands.add_parser("order", help="Deterministic tools for fa-order")
    order_actions = order_command.add_subparsers(dest="action", required=True)
    context = order_actions.add_parser("context")
    context.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    context.add_argument("--json", action="store_true")
    progress = order_actions.add_parser("progress", help="Record local order preparation facts")
    progress_steps = progress.add_subparsers(dest="step", required=True)
    for step in cli.order.PROGRESS_STEPS:
        command = progress_steps.add_parser(step)
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        if step == "start":
            command.add_argument("--title", required=True)
        elif step in {"ledger", "chunk"}:
            command.add_argument("--add", required=True)
        elif step == "cell":
            command.add_argument("--chunk", required=True)
            command.add_argument("--slot", choices=cli.order.PROGRESS_SLOTS, required=True)
            cell = command.add_mutually_exclusive_group(required=True)
            cell.add_argument("--ask")
            cell.add_argument("--filled", action="store_true")
        elif step == "tests":
            command.add_argument("--done", type=int, required=True)
            command.add_argument("--total", type=int, required=True)
        elif step == "publish":
            command.add_argument("--issue", type=int, required=True)
    preread = order_actions.add_parser(
        "preread", help="Ask each issue's implementer, read-only, to list unknowns before approval")
    preread.add_argument("plan", type=cli.Path)
    preread.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    preread.add_argument("--json", action="store_true")
    say = lambda key: cli.text(key, _help_language())  # noqa: E731
    note = order_actions.add_parser("note", help=say("order_notes_help"))
    note.add_argument("--defer", required=True, help=say("order_notes_defer_help"))
    note.add_argument("--kind", choices=("decision", "idea", "rule"), required=True, help=say("order_notes_kind_help"))
    note.add_argument("--text", required=True, help=say("order_notes_text_help"))
    note.add_argument("--replaces", help=say("order_notes_replaces_help"))
    note.add_argument("--replace", help=say("order_flush_replace_help"))
    pending = order_actions.add_parser("pending", help=say("order_notes_pending_help"))
    flush = order_actions.add_parser("flush", help=say("order_flush_help"))
    flush.add_argument("--boundary", required=True, help=say("order_flush_boundary_help"))
    for command in (note, pending, flush):
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        command.add_argument("--json", action="store_true")
    prepare = order_actions.add_parser("prepare", help=say("prepare_help"), description=say("prepare_help"))
    prepare.add_argument("plan", type=cli.Path)
    prepare.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    prepare.add_argument("--status", action="store_true", help=say("prepare_help_status"))
    prepare.add_argument("--json", action="store_true")
    perspectives = order_actions.add_parser("perspectives", help=say("perspectives_help"),
                                            description=say("perspectives_help"))
    perspectives.add_argument("target", help=say("perspectives_help_target"))
    perspectives.add_argument("question", nargs="?", help=say("perspectives_help_question"))
    perspectives.add_argument("--changed", choices=("yes", "no"), help=say("perspectives_help_changed"))
    perspectives.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    perspectives.add_argument("--json", action="store_true")
    publish = order_actions.add_parser(
        "publish", formatter_class=cli.argparse.RawDescriptionHelpFormatter, epilog=PUBLISH_EPILOG)
    publish.add_argument("plan", type=cli.Path)
    publish.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    publish.add_argument("--approve", action="store_true")
    publish.add_argument("--approve-keys", type=cli._comma_keys, default=[])
    publish.add_argument("--approve-numbers", type=cli._comma_numbers, default=[])
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--defer", help=say("order_submission_defer_help"))
    publish.add_argument("--urgent", action="store_true", help=say("order_submission_urgent_help"))


def _preread(args, root) -> int:
    cli = facade()
    config = cli.load_config(root)
    try:
        # The repository publish will name in the issue body, so preread can bind its proposal to it.
        repo = config.repo or cli.GitRepo(root).remote_slug()
    except cli.GitError:
        repo = None
    results = cli.order.preread(cli.order.read_plan(args.plan), root, config,
                                lambda: cli.GhTracker(repo or cli.GitRepo(root).remote_slug()), repo=repo)
    if args.json:
        print(cli.json.dumps({"issues": results}, ensure_ascii=False))
    else:
        for result in results:
            print(f"{result['key']}: " + ("no unknowns" if not result["unknowns"] else
                                          f"{len(result['unknowns'])} unknown(s)"))
            for unknown in result["unknowns"]:
                print(f"  - {unknown}")
            if "current" in result:
                print(result["current"])
    return 0


def _prepare(args, root) -> int:
    from foragent import order_prepare

    cli = facade()
    config, repo, store = repo_store(root)
    rows = order_prepare.prepare(cli.order.read_plan(args.plan), root, config, cli.GitRepo(root), store,
                                 lambda: cli.GhTracker(repo), status=args.status, plan_path=args.plan)
    if args.json:
        print(cli.json.dumps({"issues": rows}, ensure_ascii=False))
    else:
        for row in rows:
            print(cli.text("prepare_row", config.language, key=row["key"], status=row["status"],
                           reason=row["reason"] or "-"))
            for question in row["questions"]:
                print(cli.text("prepare_row_question", config.language, text=question))
    return 0


def _perspectives(args, root) -> int:
    from foragent import order_perspectives
    from foragent.i18n import text

    cli = facade()
    config, _repo, store = repo_store(root)
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    seat = lambda name: say(f"perspectives_seat_{name}")  # noqa: E731
    if args.target == "mark":
        if not args.question or args.changed is None:
            raise cli.order.PlanError(say("perspectives_mark_usage"))
        question = order_perspectives.mark(store, args.question, args.changed == "yes", config.language)
        print(cli.json.dumps(question, ensure_ascii=False) if args.json else
              say(f"perspectives_marked_{args.changed}", id=args.question))
        return 0
    if args.question is not None or args.changed is not None:
        raise cli.order.PlanError(say("perspectives_mark_only"))
    if args.target == "stats":
        result = order_perspectives.stats(store)
        if args.json:
            print(cli.json.dumps(result, ensure_ascii=False))
        else:
            for name, row in result["perspectives"].items():
                print(say("perspectives_stats_row", seat=seat(name), **row))
        return 0
    result = order_perspectives.run(cli.order.read_plan(cli.Path(args.target)), root, config, store)
    if args.json:
        print(cli.json.dumps(result, ensure_ascii=False))
    else:
        for question in result["questions"]:
            print(say("perspectives_question", seat=seat(question["perspective"]), **{
                key: question[key] for key in ("executor", "id", "text")}))
        for failure in result["failures"]:
            print(say("perspectives_failed", seat=seat(failure["perspective"]),
                      executor=failure["executor"] or "-", error=failure["error"]))
        print(say("perspectives_summary", count=len(result["questions"]), plan_hash=result["plan_hash"][:12]))
    if len(result["failures"]) == len(order_perspectives.SEATS):
        print(say("perspectives_none_ran"), file=cli.sys.stderr)
        return 1
    return 0


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if args.action == "flush":
        from foragent import order_flush
        return order_flush.run(args, root)
    if args.action in {"note", "pending"}:
        from foragent import order_notes
        return order_notes.run(args, root)
    if args.action == "progress":
        _config, _repo, store = repo_store(root)
        fields = {name: getattr(args, name) for name in
                  ("title", "add", "chunk", "slot", "ask", "filled", "done", "total", "issue")
                  if hasattr(args, name)}
        print(cli.json.dumps(cli.order.progress(store, args.step, **fields), ensure_ascii=False))
        return 0
    if args.action == "preread":
        return _preread(args, root)
    if args.action == "perspectives":
        return _perspectives(args, root)
    if args.action == "prepare":
        return _prepare(args, root)
    git = cli.GitRepo(root)
    config = cli.load_config(root)
    repo = config.repo or git.remote_slug()
    tracker = cli.GhTracker(repo)
    if args.action == "context":
        print(cli.json.dumps(cli.order.context(root, config, tracker), ensure_ascii=False))
        return 0
    tracker = cli.RunContext(repo=repo, repo_root=root, tracker=tracker,
                             store=cli.StateStore.for_repo(repo), config=config, git=git,
                             now=lambda: cli.datetime.now(cli.UTC)).tracker
    plan = cli.order.read_plan(args.plan)
    report = cli.order.publish(
        plan, root, config, tracker, repo=repo, git=git,
        approve=args.approve, approve_keys=args.approve_keys,
        approve_numbers=args.approve_numbers, dry_run=args.dry_run, defer=args.defer, urgent=args.urgent,
    )
    print(report.summary())
    if not args.dry_run and report.waiting_for is None:
        cli._open_lane_view(root)
    return 0

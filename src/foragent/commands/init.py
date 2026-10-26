"""`fa init`: set up a repository for Foragent."""

from foragent.commands import facade

ORDER = 50


def add(commands) -> None:
    cli = facade()
    init = commands.add_parser("init", help="Set up this repository for Foragent")
    init.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    init.add_argument("--approver", nargs="+", action="extend", dest="approvers")
    init.add_argument("--review", choices=("fast", "batch", "careful"))
    init.add_argument("--executor", choices=("codex", "claude"))
    init.add_argument("--run-hours")
    init.add_argument("--language")
    init.add_argument("--no-push", action="store_true")
    init.add_argument("--no-launchd", action="store_true")
    init.add_argument("--no-board", action="store_true")
    init.add_argument("--board-map", help="Existing board columns: Backlog=Todo,Up next=Todo,In progress=Doing,Your turn=Review,Done=Done")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--statusline", choices=("yes", "no"),
                      help="Answer the Claude statusline question without a prompt")


def _offer_statusline(answer: str | None) -> None:
    from foragent import statusline
    try:
        outcome = statusline.offer_install(input if answer is None else lambda _prompt: answer)
    except OSError as exc:
        print(f"Claude statusline: not installed ({exc}); fa init will ask again next time.")
        return
    if outcome == "installed":
        print("Claude statusline: installed (your previous statusline still shows).")
    elif outcome == "declined":
        print("Claude statusline: not installed; Foragent will not ask again.")
    elif outcome == "unanswered":
        print("Claude statusline: not asked (no terminal); rerun with --statusline yes|no.")


def _offer_upstream_reports(root) -> None:
    """Ask once, default no, whether upstream error reports may be proposed; remember either answer."""
    from foragent.config import load_config, set_upstream_reports
    from foragent.upstream_report import UPSTREAM

    cli = facade()
    try:
        # Asked only after setup wrote a usable config; the answer itself is global.
        # Read the file directly so the facade's config hook sees only the view's read.
        language = load_config(root).language
        if cli.load_global_config().upstream_reports is not None:
            return
    except cli.ConfigError:
        return
    try:
        answer = input(cli.text("upstream_reports_question", language, upstream=UPSTREAM))
    except (EOFError, OSError):
        print(cli.text("upstream_reports_unanswered", language))
        return
    enabled = answer.strip().lower() in {"y", "yes"}
    set_upstream_reports(enabled)
    print(cli.text("upstream_reports_on" if enabled else "upstream_reports_off", language))


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    report = cli.init_repo(
        root, approvers=args.approvers, review=args.review, executor=args.executor,
        run_hours=args.run_hours, no_push=args.no_push, no_launchd=args.no_launchd,
        dry_run=args.dry_run, language=args.language, no_board=args.no_board,
        board_map=args.board_map,
    )
    print(report.summary())
    if not args.dry_run:
        cli._offer_statusline(args.statusline)
        _offer_upstream_reports(report.normalized_root or root)
        cli._open_lane_view(report.normalized_root or root)
    return 0

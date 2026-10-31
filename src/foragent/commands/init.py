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
    init.add_argument("--claude-quota", choices=("keep", "burn"),
                      help="Default Claude quota mode: keep stops at keep_percent, burn runs to the limit")
    init.add_argument("--statusline", choices=("yes", "no"),
                      help="Answer the Claude statusline question without a prompt")
    init.add_argument("--fa-allow", choices=("yes", "no"),
                      help="Answer the project Bash(fa:*) permission offer without a prompt")
    init.add_argument("--starter", choices=("yes", "no"),
                      help="Answer the starter order offer without a prompt")


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
    elif outcome == "hooked":
        # Installed before the session-start summary existed: added quietly, said in one line.
        cli = facade()
        try:
            language = cli.load_config(cli.Path.cwd()).language
        except Exception:  # Outside a readable Foragent config, speak English.
            language = "en"
        print(cli.text("statusline_hook_added", language))


def _offer_fa_allow(root, answer: str | None) -> None:
    """Offer the one project rule that lets Claude run fa commands; only an explicit yes adds it."""
    from foragent.init import FA_ALLOW_RULE, offer_fa_allow

    try:
        outcome = offer_fa_allow(root, input if answer is None else lambda _prompt: answer,
                                 require_settings=answer is None)
    except OSError as exc:
        print(f"FA commands: not allowed ({exc}); fa init will ask again next time.")
        return
    if outcome == "added":
        print(f"FA commands: allowed in this project ({FA_ALLOW_RULE} in .claude/settings.local.json).")
    elif outcome == "declined":
        print("FA commands: not allowed; Claude keeps asking per command and fa init will not ask again.")
    elif outcome == "unanswered":
        print("FA commands: not asked (no terminal); rerun with --fa-allow yes|no.")
    elif outcome == "busy":
        print("FA commands: not asked; another fa init is asking about this project now.")
    elif outcome == "unreadable":
        print("FA commands: not asked (.claude/settings.local.json is not a JSON object).")


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
    try:
        set_upstream_reports(enabled)
    except (OSError, cli.ConfigError) as exc:
        # Setup itself succeeded; say so in the repository's language and ask again next time.
        print(cli.text("upstream_reports_save_failed", language, error=" ".join(str(exc).split())[:300]))
        return
    print(cli.text("upstream_reports_on" if enabled else "upstream_reports_off", language))


def _offer_starter(root, tracker, answer: str | None) -> None:
    """Offer one tiny first order when the project has no Foragent work; store nothing."""
    from foragent import starter
    from foragent.config import load_config

    cli = facade()
    try:
        language = load_config(root).language
        if tracker is None or not starter.eligible(tracker):
            return
    except Exception:  # No readable config or tracker facts: skip the offer quietly.
        return
    if answer is None:
        try:
            answer = input(cli.text("starter_question", language))
        except (EOFError, OSError):
            return
    if answer.strip().lower() in {"y", "yes"}:
        print(cli.text("starter_next_step", language, example=cli.text("starter_example", language)))


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    report = cli.init_repo(
        root, approvers=args.approvers, review=args.review, executor=args.executor,
        run_hours=args.run_hours, no_push=args.no_push, no_launchd=args.no_launchd,
        dry_run=args.dry_run, language=args.language, no_board=args.no_board,
        board_map=args.board_map,
        # Only an explicit flag reaches setup; without it no claude_quota block is written.
        **({"claude_quota": args.claude_quota} if args.claude_quota else {}),
    )
    print(report.summary())
    if not args.dry_run:
        cli._offer_statusline(args.statusline)
        _offer_fa_allow(report.normalized_root or root, args.fa_allow)
        _offer_upstream_reports(report.normalized_root or root)
        _offer_starter(report.normalized_root or root, getattr(report, "tracker", None), args.starter)
        cli._open_lane_view(report.normalized_root or root)
    return 0

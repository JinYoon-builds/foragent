"""`fa report`: prepare an upstream error report, then send it only on approval (#419)."""

from foragent.commands import facade, repo_store

ORDER = 157


def _help_language(root) -> str:
    """The language of the repository at ``root``; English if it has no readable config.

    Read the file directly: every `fa` command builds this parser and must neither fail
    nor be observed through the facade's config hook because of it.
    """
    from foragent.config import load_config
    try:
        return load_config(root).language
    except (ValueError, TypeError, OSError):
        return "en"


def _repo_root_in(args) -> str | None:
    """The --repo-root value among a subcommand's arguments, before argparse reads them."""
    args = list(args or [])
    for index, arg in enumerate(args):
        if arg == "--repo-root" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--repo-root="):
            return arg.split("=", 1)[1]
    return None


def add(commands) -> None:
    cli = facade()
    report = commands.add_parser("report", help=cli.text("report_help", _help_language(cli.Path.cwd())))
    actions = report.add_subparsers(dest="action", required=True)
    keyed = []  # (object with .help, message key), localized once the repository is known.

    def say(target, key):
        keyed.append((target, key))
        return target

    propose = actions.add_parser("propose", help="")
    say(actions._choices_actions[-1], "report_help_propose")
    say(propose.add_argument("--from", dest="source", required=True, metavar="flag:N|event:N"), "report_help_from")
    say(propose.add_argument("--json", action="store_true"), "report_help_json")
    propose.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    send = actions.add_parser("send", help="")
    say(actions._choices_actions[-1], "report_help_send")
    say(send.add_argument("report_id"), "report_help_id")
    say(send.add_argument("--yes", action="store_true"), "report_help_yes")
    send.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())

    def localize(args) -> None:
        # Help is printed while argparse reads these arguments, so the repository named by
        # --repo-root (anywhere after `report`) decides the language before parsing starts.
        root = _repo_root_in(args)
        language = _help_language(cli.Path(root) if root else cli.Path.cwd())
        for target, key in keyed:
            target.help = cli.text(key, language)

    localize(None)
    parse = report.parse_known_args

    def parse_known_args(args=None, namespace=None):
        localize(args)
        return parse(args, namespace)

    report.parse_known_args = parse_known_args


def _language(root) -> str:
    cli = facade()
    try:
        return cli.load_config(root).language
    except cli.ConfigError:
        return "en"


def _print_report(report: dict) -> None:
    print(report["title"])
    print()
    print(report["body"], end="")


def run(args) -> int:
    from foragent import upstream_report
    from foragent.config import config_key_names

    cli = facade()
    root = args.repo_root.resolve()
    language = _language(root)
    values = {"source": getattr(args, "source", ""), "report_id": getattr(args, "report_id", ""),
              "upstream": upstream_report.UPSTREAM}
    try:
        # Consent is the user's, so it is read from the global config on every call.
        try:
            allowed = cli.load_global_config().upstream_reports is True
        except cli.ConfigError as exc:
            raise upstream_report.ReportError("report_config_invalid",
                                              error=upstream_report._detail(exc)) from exc
        if not allowed:
            raise upstream_report.ReportError("report_not_allowed")
        try:
            _config, _repo, store = repo_store(root)
            keys = config_key_names(root) if args.action == "propose" else []
        except (cli.ConfigError, cli.GitError, OSError) as exc:
            raise upstream_report.ReportError("report_repo_invalid", error=upstream_report._detail(exc)) from exc
        if args.action == "propose":
            report = upstream_report.propose(store, args.source, keys, language)
            if args.json:
                print(cli.json.dumps(report, ensure_ascii=False))
            else:
                _print_report(report)
                print()
                print(cli.text("report_send_hint", language, report_id=report["id"]))
            return 0
        report = upstream_report.load(store, args.report_id)
        if report.get("sent"):
            print(cli.text("report_already_sent", language, report_id=report["id"], url=report["sent"]["url"]))
            return 1
        if not args.yes:
            _print_report(report)
            try:
                answer = input(cli.text("report_send_question", language, **values))
            except (EOFError, OSError):
                answer = ""
            if answer.strip().lower() not in {"y", "yes"}:
                print(cli.text("report_declined", language))
                return 1
        action, url = upstream_report.send(store, report, cli.GhTracker(upstream_report.UPSTREAM))
        print(cli.text("report_commented" if action == "comment" else "report_created", language, url=url))
        return 0
    except upstream_report.ReportError as exc:
        path = cli.Path.home() / ".config/foragent/config.yml"
        print("foragent: " + cli.text(str(exc), language, **{"path": path, **values, **exc.values}),
              file=cli.sys.stderr)
        return 1

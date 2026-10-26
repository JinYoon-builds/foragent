"""`fa report`: prepare an upstream error report, then send it only on approval (#419)."""

from foragent.commands import facade, repo_store

ORDER = 157


def add(commands) -> None:
    cli = facade()
    report = commands.add_parser("report", help="Prepare and send an upstream Foragent error report")
    actions = report.add_subparsers(dest="action", required=True)
    propose = actions.add_parser("propose", help="Prepare a report from a local record and show all of it")
    propose.add_argument("--from", dest="source", required=True, metavar="flag:N|event:N",
                         help="flag:N is issue N's flag; event:N is line N of the orchestrator journal")
    propose.add_argument("--json", action="store_true")
    propose.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    send = actions.add_parser("send", help="Send one prepared report after your approval")
    send.add_argument("report_id")
    send.add_argument("--yes", action="store_true", help="Approve sending this report without a prompt")
    send.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


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
        if cli.load_global_config().upstream_reports is not True:
            raise upstream_report.ReportError("report_not_allowed")
        _config, _repo, store = repo_store(root)
        if args.action == "propose":
            report = upstream_report.propose(store, args.source, config_key_names(root))
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
        print("foragent: " + cli.text(str(exc), language, path=path, **values), file=cli.sys.stderr)
        return 1

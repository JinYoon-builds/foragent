"""`fa brief`: the start-of-session facts for planning."""

from foragent.commands import facade, repo_store

ORDER = 150


def add(commands) -> None:
    cli = facade()
    briefing = commands.add_parser("brief", help="Show the start-of-session facts for planning")
    briefing.add_argument("--json", action="store_true")
    briefing.add_argument("--report", action="store_true",
                          help="Print one report of what finished since the previous report, and record it")
    briefing.add_argument("--star", metavar="ANSWER", help="Answer the one-time GitHub star request (yes stars)")
    briefing.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    configured = cli.brief.configured(root)
    if args.star is not None:
        language = cli.load_config(root).language if configured else "en"
        if args.json:
            print(cli.text("star_json", language), file=cli.sys.stderr)
            return 2
        print(cli.star.answer(args.star, language))
        return 0
    if not configured:
        facts = cli.brief.unconfigured()
        if args.json:
            print(cli.json.dumps(facts))
            return 0
        if args.report:
            print(cli.brief_report.welcome(cli.os.environ.get("LANG", "en")[:2]))
            return 0
        print(cli.brief.render(facts, cli.os.environ.get("LANG", "en")[:2]))
        _print_offer(cli, "en")
        return 0
    config, _repo, store = repo_store(root)
    if args.report and not args.json and config.presence["report"]:
        now = cli.datetime.now(cli.UTC)
        print(cli.brief_report.render(cli.brief_report.collect(store, now), config.language))
        cli.brief_report.record_report(store, now)
        return 0
    facts = cli.brief.collect(store, config, cli.datetime.now(cli.UTC))
    if offer := cli.update_check.check(config):
        facts["update"] = offer
    if args.json:
        print(cli.json.dumps(facts, ensure_ascii=False))
        return 0
    print(cli.brief.render(facts, config.language))
    _print_offer(cli, config.language)
    return 0


def _print_offer(cli, language: str) -> None:
    """The one-time star request; human briefing output only."""
    question = cli.star.offer(language)
    if question:
        print(question)

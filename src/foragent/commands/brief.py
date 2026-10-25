"""`fa brief`: the start-of-session facts for planning."""

from foragent.commands import facade, repo_store

ORDER = 150


def add(commands) -> None:
    cli = facade()
    briefing = commands.add_parser("brief", help="Show the start-of-session facts for planning")
    briefing.add_argument("--json", action="store_true")
    briefing.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if not cli.brief.configured(root):
        facts = cli.brief.unconfigured()
        print(cli.json.dumps(facts) if args.json else cli.brief.render(facts, cli.os.environ.get("LANG", "en")[:2]))
        return 0
    config, _repo, store = repo_store(root)
    facts = cli.brief.collect(store, config, cli.datetime.now(cli.UTC))
    print(cli.json.dumps(facts, ensure_ascii=False) if args.json
          else facade().brief.render(facts, config.language))
    return 0

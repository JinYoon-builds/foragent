"""`fa settings list|get|set`: team, me and this computer's settings, no LLM (#160)."""

from foragent.commands import facade

ORDER = 72


def _help_language() -> str:
    from foragent.commands.order import _help_language as order_help_language

    return order_help_language()


def add(commands) -> None:
    cli = facade()
    say = lambda key: cli.text(key, _help_language())
    command = commands.add_parser("settings", help=say("settings_help"))
    actions = command.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help=say("settings_list_help"))
    getting = actions.add_parser("get", help=say("settings_get_help"))
    getting.add_argument("key")
    setting = actions.add_parser("set", help=say("settings_set_help"))
    setting.add_argument("key")
    setting.add_argument("value", nargs="?")
    setting.add_argument("--unset", action="store_true", help=say("settings_unset_help"))
    for parser in (listing, getting, setting):
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    from foragent import settings, settings_write

    cli = facade()
    root = args.repo_root.resolve()
    if not (root / ".foragent.yml").is_file():
        raise cli.ConfigError(cli.text("settings_not_repo", settings.language(root), path=root))
    language = settings.language(root)
    if args.action == "list":
        items = settings.list_items(root, language)
        if args.json:
            print(cli.json.dumps(items, ensure_ascii=False))
        else:
            for entry in items:
                print("  ".join((entry["scope"], entry["key"], settings.plain(entry["value"]),
                                 entry["description"])))
        return 0
    if args.action == "get":
        entry = settings.get_item(root, args.key, language)
    else:
        entry = settings_write.set_value(root, args.key, args.value, unset=args.unset, language=language)
    if args.json:
        print(cli.json.dumps(entry, ensure_ascii=False))
    elif args.action == "get":
        print(settings.plain(entry["value"]))
    else:
        print("  ".join((entry["scope"], entry["key"], settings.plain(entry["value"]))))
    return 0

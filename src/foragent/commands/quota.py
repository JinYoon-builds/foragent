"""`fa quota`: choose how far gate-started Claude runs use the plan (#107)."""

from foragent.commands import facade, repo_store

ORDER = 142


def add(commands) -> None:
    cli = facade()
    quota = commands.add_parser("quota", help="Choose how far Claude runs use the plan")
    actions = quota.add_subparsers(dest="action", required=True)
    mode = actions.add_parser("mode", help="Show or set keep (stop at keep_percent) or burn (run to the limit)")
    mode.add_argument("mode", nargs="?", choices=("keep", "burn"))
    mode.add_argument("--clear", action="store_true", help="Remove the local override; the config applies")
    mode.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    from foragent import scheduler_quota

    cli = facade()
    config, _repo, store = repo_store(args.repo_root.resolve())
    language = config.language
    if args.clear and args.mode:
        print("foragent: " + cli.text("quota_mode_conflict", language), file=cli.sys.stderr)
        return 2
    if args.clear:
        store.set_claude_quota_mode(None)
        print(cli.text("quota_mode_cleared", language, mode=config.claude_quota["mode"]))
        return 0
    if args.mode:
        store.set_claude_quota_mode(args.mode)
        print(cli.text("quota_mode_set", language, mode=args.mode))
        return 0
    mode, source = scheduler_quota.quota_mode(store, config)
    percent = scheduler_quota.claude_percent(store)
    usage = cli.text("quota_usage_unknown", language) if percent is None else f"{percent:g}%"
    print(cli.text("quota_mode_status", language, mode=mode, source=source,
                   keep_percent=config.claude_quota["keep_percent"], usage=usage))
    return 0

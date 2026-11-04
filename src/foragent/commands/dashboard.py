"""`fa dashboard`: the person's name for `fa gate watch`, with the same flags and behaviour.

With an issue number, or inside an issue's lane worktree, it shows that one issue instead.
"""

from foragent.commands import facade

ORDER = 112


def add(commands) -> None:
    from foragent.commands.gate import add_watch_flags

    command = commands.add_parser("dashboard", help="Show the live dashboard of lanes and work")
    command.add_argument("number", nargs="?", type=int,
                         help="Show only this issue (inferred inside its lane worktree)")
    add_watch_flags(command)


def run(args) -> int:
    from foragent import watch_issue
    from foragent.commands import gate

    number = args.number
    if number is None and not args.open:
        number = watch_issue.infer(args.repo_root)
    if number is None or args.open:
        args.action = "watch"
        return gate.run(args)
    return _issue(args, number)


def _issue(args, number: int) -> int:
    """The focused view: the same local snapshot as the full dashboard, never refreshed from GitHub."""
    from foragent import watch_issue

    cli = facade()
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    repo = config.repo or cli.GitRepo(root).remote_slug()
    tracker = cli.GhTracker(repo)
    store = cli.StateStore.for_repo(repo)
    now = lambda: cli.datetime.now(cli.UTC)

    def read():
        with cli.watch.collection_root(root):
            return cli.watch.collect(cli._status(tracker, store, config), tracker, store, config, now())

    return watch_issue.run(read, store, config, root, number, once=args.once, now=now)

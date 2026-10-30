"""`fa notify`: send an alert through the configured channel."""

from foragent.commands import facade

ORDER = 170


def add(commands) -> None:
    cli = facade()
    notification = commands.add_parser("notify", help="Send an alert through the configured channel")
    notification.add_argument("--title", required=True)
    notification.add_argument("--line", required=True)
    notification.add_argument("--link")
    notification.add_argument("--kind", choices=cli.notify.KINDS,
                              help="What this alert is; notify_filter decisions pushes only "
                                   + ", ".join(cli.notify.DECISION_KINDS))
    notification.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    try:
        repo = config.repo or cli.GitRepo(root).remote_slug()
    except (cli.GitError, OSError):
        repo = None  # The shared entry reads the checkout's origin files, or reports the failure.
    with cli.notify.sending(args.kind, root) as outcome:
        sent = cli.notify.send(cli.replace(config, repo=repo), args.title, args.line, args.link)
    if sent is not False:
        return 0
    print(cli.text("notify_failed", config.language, error=outcome.error or "unknown"), file=cli.sys.stderr)
    return 1

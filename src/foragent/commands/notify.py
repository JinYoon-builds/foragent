"""`fa notify`: send an alert through the configured channel."""

from dataclasses import replace
from pathlib import Path

from foragent.commands import facade

ORDER = 170


def add(commands) -> None:
    notification = commands.add_parser("notify", help="Send an alert through the configured channel")
    notification.add_argument("--title", required=True)
    notification.add_argument("--line", required=True)
    notification.add_argument("--link")
    notification.add_argument("--repo-root", type=Path, default=Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    repo = config.repo or cli.GitRepo(root).remote_slug()
    cli.notify.send(replace(config, repo=repo), args.title, args.line, args.link)
    return 0

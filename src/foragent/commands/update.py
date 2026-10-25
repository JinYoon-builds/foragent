"""`fa update`: install the latest Foragent release and restart the gates."""

from foragent.commands import facade

ORDER = 155


def add(commands) -> None:
    cli = facade()
    updating = commands.add_parser("update", help="Install the latest Foragent release and restart the gates")
    updating.add_argument("--repo-root", type=cli.Path, default=None)


def run(args) -> int:
    return facade().update_check.update(args.repo_root)

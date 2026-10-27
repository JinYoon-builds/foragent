"""`fa adopt`: prepare an existing repository for Foragent."""

from foragent.commands import facade

ORDER = 20


def add(commands) -> None:
    cli = facade()
    adopt_command = commands.add_parser("adopt", help="Prepare an existing repository for Foragent")
    adopt_actions = adopt_command.add_subparsers(dest="action", required=True)
    for name in ("survey", "baseline", "ci"):
        command = adopt_actions.add_parser(name)
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        command.add_argument("--json", action="store_true")
        if name == "baseline":
            command.add_argument("--command", dest="test_command")
            command.add_argument("--timeout", type=int, default=900)
        elif name == "ci":
            command.add_argument("--exclude-acceptance", action="store_true")


def run(args) -> int:
    return facade().adopt.run(args)

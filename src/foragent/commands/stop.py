"""`fa stop`: the person's one emergency stop; FA's shared save-and-stop for every lane (#1492).

This entrance only submits the request to `fa orch stop --all --keep-work` and prints the
recorded result; the runner that owns each run saves and finishes it, as for the bulk command.
"""

from foragent.commands import facade

ORDER = 113


def add(commands) -> None:
    cli = facade()
    stop = commands.add_parser("stop", help="Stop every lane now; running work is saved and kept")
    stop.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    stop.add_argument("--why", help="Why you stop FA; kept in the journal (default: you typed fa stop)")


def run(args) -> int:
    from foragent.commands import orch

    cli = facade()
    why = args.why or cli.text("stop_default_why", cli._project_language(args.repo_root))
    args.command, args.action, args.all, args.number, args.keep_work, args.why = (
        "orch", "stop", True, None, True, why)
    return orch.run(args)

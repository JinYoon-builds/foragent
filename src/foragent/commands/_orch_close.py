"""Close superseded work through the FA write boundary."""

from foragent.commands import facade

ACTIONS = {"close"}


def add_parsers(orch_actions) -> None:
    cli = facade()
    command = orch_actions.add_parser("close", help=cli.text("orch_close_help"))
    command.add_argument("number", type=int)
    command.add_argument("--superseded-by", type=int, required=True)
    command.add_argument("--why", required=True)
    command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(ctx, args) -> int:
    from foragent import orch_close

    print(orch_close.close(ctx, args.number, args.superseded_by, args.why))
    return 0

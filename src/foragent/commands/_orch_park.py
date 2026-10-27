"""`fa orch park` and `fa orch unpark`; split from commands/orch.py to keep it short.

Discovery skips this module because its name starts with an underscore.
"""

from foragent.commands import facade

ACTIONS = {"park", "unpark"}


def add_parsers(orch_actions) -> None:
    cli = facade()
    park = orch_actions.add_parser("park", help="Record the orchestrator's decision to park a blocked or held issue")
    park.add_argument("number", type=int)
    park.add_argument("--why", required=True, help="Why the issue is parked")
    park.add_argument("--for-person", action="store_true",
                      help="Also mark the issue as waiting for a person's decision (waiting-person)")
    unpark = orch_actions.add_parser("unpark", help="Remove waiting-person from a parked issue")
    unpark.add_argument("number", type=int)
    for command in (park, unpark):
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(ctx, args) -> int:
    from foragent import orch_park

    if args.action == "park":
        print(orch_park.park(ctx, args.number, args.why, for_person=args.for_person))
    else:
        print(orch_park.unpark(ctx, args.number))
    return 0

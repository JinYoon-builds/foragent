"""`fa bench`: record and report benchmark scenarios."""

from foragent.commands import facade

ORDER = 30


def add(commands) -> None:
    cli = facade()
    bench_command = commands.add_parser("bench", help="Record and report benchmark scenarios")
    bench_actions = bench_command.add_subparsers(dest="action", required=True)
    record = bench_actions.add_parser("record")
    record.add_argument("scenario_dir", type=cli.Path)
    record.add_argument("--arm", choices=("A", "B", "C"), required=True)
    record.add_argument("--repo", type=cli.Path, required=True)
    record.add_argument("--started", required=True)
    record.add_argument("--finished")
    record.add_argument("--state-dir", type=cli.Path)
    record.add_argument("--tokens", type=int)
    record.add_argument("--interventions", type=int)
    report = bench_actions.add_parser("report")
    report.add_argument("scenario_dir", type=cli.Path)
    report.add_argument("--json", action="store_true")


def run(args) -> int:
    return facade().bench.run(args)

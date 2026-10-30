"""`fa orch seat --json`: read-only view of the orchestrator seat lease (#962).

Discovery skips this module because its name starts with an underscore.
"""

from foragent.commands import facade, repo_store


def add_parsers(orch_actions) -> None:
    cli = facade()
    seat = orch_actions.add_parser("seat", help=cli.text("orch_seat_help"))
    seat.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    seat.add_argument("--json", action="store_true", help=cli.text("orch_seat_json_help"))
    seat.set_defaults(handler=run)


def run(args) -> int:
    from foragent import orch_seat

    cli = facade()
    config, _repo, store = repo_store(args.repo_root.resolve())
    report = orch_seat.report(store)
    if args.json:
        print(cli.json.dumps(report, ensure_ascii=False))
    else:
        print(cli.text(_line(report), config.language, **(report["holder"] or {})))
    return 0


def _line(report: dict) -> str:
    if report["holder"] is None:
        return "orch_seat_free"
    return {True: "orch_seat_live", False: "orch_seat_gone"}.get(report["alive"], "orch_seat_unknown")

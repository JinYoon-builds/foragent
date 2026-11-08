"""`fa orch main-write retire ID --why TEXT`: retire one blocked main-write request (q86).

Discovery skips this module because its name starts with an underscore.
"""

from foragent.commands import facade, repo_store


def add_parsers(orch_actions) -> None:
    cli = facade()
    main_write = orch_actions.add_parser("main-write", help=cli.text("orch_main_write_help"))
    verbs = main_write.add_subparsers(dest="main_write_action", required=True)
    retire = verbs.add_parser("retire", help=cli.text("orch_main_write_retire_help"))
    retire.add_argument("id")
    retire.add_argument("--why", required=True, help=cli.text("orch_main_write_why_help"))
    retire.add_argument("--by", default="orchestrator", help=cli.text("orch_main_write_by_help"))
    retire.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    retire.set_defaults(handler=run)


def run(args) -> int:
    from foragent import main_write_requests

    cli = facade()
    config, _repo, store = repo_store(args.repo_root.resolve())
    try:
        row = main_write_requests.retire(store, args.id, args.why, args.by)
    except main_write_requests.Refused as exc:
        print(str(exc), file=cli.sys.stderr)
        return 1
    print(cli.text("orch_main_write_retired", config.language, id=row["id"], title=row["title"], why=args.why))
    cli.kick_tick(args.repo_root.resolve())  # The owner's queue continues past it in the next pass.
    return 0

"""`fa decisions`: decision links and derived completion from local facts only (#1025)."""

from foragent.commands import facade, repo_store

ORDER = 142


def add(commands) -> None:
    from foragent.commands.order import _help_language

    cli = facade()
    language = _help_language()
    command = commands.add_parser("decisions", help=cli.text("decisions_help", language))
    command.add_argument("--json", action="store_true", help=cli.text("decisions_json_help", language))
    command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    actions = command.add_subparsers(dest="action")
    sweep = actions.add_parser("_sweep", help=cli.argparse.SUPPRESS)
    sweep.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    confirm = actions.add_parser("add", help=cli.text("decision_sweep_add_help", language))
    confirm.add_argument("candidate", help=cli.text("decision_sweep_candidate_help", language))
    confirm.add_argument("--text", required=True, help=cli.text("decision_sweep_text_help", language))
    confirm.add_argument("--defer", required=True, help=cli.text("order_notes_defer_help", language))
    confirm.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    confirm.add_argument("--json", action="store_true")


def run(args) -> int:
    from foragent import decisions

    cli = facade()
    if args.action == "_sweep":
        from foragent import decision_sweep
        return decision_sweep.hook(args.repo_root)
    root = args.repo_root.resolve()
    config, _repo, store = repo_store(root)
    if args.action == "add":
        if "FORAGENT_ROLE" in cli.os.environ:
            print(cli.text("launched_person_only", config.language, command="decisions add"), file=cli.sys.stderr)
            return 1
        from foragent import decision_sweep
        result = decision_sweep.confirm(store, args.candidate, args.text, args.defer, config.language)
        print(cli.json.dumps(result, ensure_ascii=False) if args.json else
              cli.text("order_notes_saved", config.language, id=result["id"], boundary=result["boundary"]))
        return 0
    ledger = root / decisions.LEDGER
    try:
        text = ledger.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        print("foragent: " + cli.text("decisions_ledger_missing", config.language, path=ledger), file=cli.sys.stderr)
        return 1
    rows = decisions.view(root, decisions.observed_rows(store._issue_view()), text)
    if args.json:
        print(cli.json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(cli.text("decisions_none", config.language))
    for row in rows:
        issues = ", ".join(f"#{n}" for n in row["issues"]) or cli.text("decisions_no_issues", config.language)
        print(cli.text("decisions_row", config.language, id=row["id"], state=row["state"], issues=issues))
    return 0

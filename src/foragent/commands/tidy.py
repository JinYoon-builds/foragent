"""`fa tidy`: check from local facts whether tidying is due."""

from foragent.commands import facade, repo_store

ORDER = 140


def add(commands) -> None:
    cli = facade()
    tidy_command = commands.add_parser("tidy", help="Check from local facts whether tidying is due")
    tidy_actions = tidy_command.add_subparsers(dest="action", required=True)
    tidy_due = tidy_actions.add_parser("due", help="Report whether tidying is due and why")
    tidy_due.add_argument("--json", action="store_true")
    tidy_due.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    tidy_done = tidy_actions.add_parser("done", help="Record a finished tidy and start a new count")
    tidy_done.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def _tidy(args, store, language: str) -> None:
    """Local tidy schedule; never reads GitHub or starts an executor."""
    cli = facade()
    if args.action == "done":
        facts = cli.tidy.done(store)
        print(cli.text("tidy_done", language, at=facts["last_tidy_at"]))
        return
    facts = cli.tidy.status(store)
    if args.json:
        print(cli.json.dumps(cli.tidy.public(facts), ensure_ascii=False))
        return
    last = facts["last_tidy_at"] or cli.text("tidy_never", language)
    key = "tidy_due" if facts["due"] else "tidy_not_due"
    print(cli.text(key, language, reason=facts["reason"], merges=facts["merges_since"], last=last))
    for line in facts["evidence"]:
        print(f"- {line}")


def run(args) -> int:
    config, _repo, store = repo_store(args.repo_root.resolve())
    facade()._tidy(args, store, config.language)
    return 0

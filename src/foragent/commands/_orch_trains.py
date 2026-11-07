"""`fa orch pause-trains --why [--until]` and `fa orch resume-trains` (orch_train_pause)."""

from foragent.commands import facade, repo_store


def add_parsers(orch_actions) -> None:
    cli = facade()
    pause = orch_actions.add_parser(
        "pause-trains", help="Pause the merge owner's train building; the running train finishes and "
                             "publications keep landing")
    pause.add_argument("--why", required=True, help="Why trains pause; fa gate status shows it")
    pause.add_argument("--until", help="Optional ISO time when the pause ends; without a timezone it is local time")
    resume = orch_actions.add_parser("resume-trains", help="Resume the merge owner's train building")
    for command in (pause, resume):
        command.add_argument("--by", choices=("human", "orchestrator"), default="human")
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        command.set_defaults(handler=run)


def run(args) -> int:
    from foragent import orch_plan, orch_train_pause
    from foragent.i18n import text

    cli = facade()
    config, _repo, store = repo_store(args.repo_root.resolve())
    language = config.language
    now = cli.datetime.now(cli.UTC)
    if args.action == "resume-trains":
        resumed = orch_train_pause.resume(store, now, args.by)
        print(text("orch_trains_resumed" if resumed else "orch_trains_not_paused", language))
        return 0
    why = " ".join(args.why.split())
    if not why:
        print(text("orch_trains_why_required", language), file=cli.sys.stderr)
        return 2
    until = None
    if args.until:
        try:
            until = orch_plan.parse_time(args.until)
        except ValueError:
            print(text("orch_plan_bad_time", language, action="pause-trains", value=args.until), file=cli.sys.stderr)
            return 2
        if until <= now:
            print(text("orch_trains_until_passed", language, until=until.isoformat()), file=cli.sys.stderr)
            return 2
    item = orch_train_pause.pause(store, now, why, until, args.by)
    print(text("orch_trains_paused", language, why=why, until=_until(item, language)))
    return 0


def _until(item: dict, language: str) -> str:
    from foragent.i18n import text

    return text("orch_trains_paused_until", language, until=item["until"]) if item.get("until") else ""

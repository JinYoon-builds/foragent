"""`fa lanes`: manage repository lanes."""

from foragent.commands import facade

ORDER = 90


def add(commands) -> None:
    cli = facade()
    lanes = commands.add_parser("lanes", help="Manage repository lanes")
    lane_actions = lanes.add_subparsers(dest="action", required=True)
    lane = lane_actions.add_parser("add")
    lane.add_argument("name")
    lane.add_argument("--paths", nargs="+", required=True)
    lane.add_argument("--skill", nargs="+", action="extend", default=[], dest="skills")
    lane.add_argument("--executor", choices=("codex", "claude"))
    lane.add_argument("--model")
    lane.add_argument("--title")
    lane.add_argument("--emoji")
    lane.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    fixed_skills = lane_actions.add_parser("skills", help="Replace the fixed skills a person chose for a lane")
    fixed_skills.add_argument("name")
    fixed_skills.add_argument("ids", nargs="*", help="Skill IDs; omit to clear the lane's fixed skills")
    fixed_skills.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    talk = lane_actions.add_parser("talk", help="Open an issue's live log or session; optionally send a message")
    talk.add_argument("number", type=int)
    talk.add_argument("--message", help="Message for the worker (running) or first prompt (finished or blocked)")
    talk.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    dreamed = lane_actions.add_parser("dreamed", help="Record that a lane's memory dream is done")
    dreamed.add_argument("name")
    dreamed.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if args.action == "talk":
        from foragent import lanes
        from foragent.commands import repo_store

        config, repo, store = repo_store(root)
        failures: list[str] = []
        try:
            print(lanes.talk(args.number, message=args.message, store=store, config=config, repo_root=root,
                             accepted=lambda message: _publish_talk(cli, repo, store, config, args.number,
                                                                    message, failures)))
        except lanes.LaneError as exc:
            if getattr(exc, "stdout", ""):  # A message still delivered goes to stdout; the failure to stderr.
                print(exc.stdout)
            raise
        finally:
            for failure in failures:  # The message stays accepted locally; only its public copy failed.
                print(failure, file=cli.sys.stderr)
        return 0
    if args.action == "dreamed":
        config = cli.load_config(root)
        store = cli.StateStore.for_repo(config.repo or cli.GitRepo(root).remote_slug())
        return cli.orch_dream.dreamed_command(store, config, args.name)
    if args.action == "skills":
        lane = cli.set_lane_skills(root, args.name, args.ids)
        print(f"Updated lane:{args.name} skills: {', '.join(lane['skills']) or '(none)'}")
        return 0
    lane = cli.add_lane(root, args.name, args.paths, skills=args.skills, executor=args.executor, model=args.model,
                        title=args.title, emoji=args.emoji)
    print(f"Created lane:{args.name} ({', '.join(lane['paths'])})")
    cli._open_lane_view(root)
    return 0


def _publish_talk(cli, repo: str, store, config, number: int, message: str, failures: list[str]) -> None:
    """Keep an accepted talk message on the issue, verbatim; a refused write waits in the outbox."""
    from foragent import orch_advice

    tracker = cli.GhTracker(repo)
    context = cli.SimpleNamespace(store=store, config=config, tracker=tracker,
                                  now=lambda: cli.datetime.now(cli.UTC))
    if error := orch_advice.publish(tracker, context, number, orch_advice.talk_body(message)):
        failures.append(f"Could not copy the message to issue #{number} now ({error}); the gate will retry it.")

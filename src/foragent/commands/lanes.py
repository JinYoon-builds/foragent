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


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if args.action == "skills":
        lane = cli.set_lane_skills(root, args.name, args.ids)
        print(f"Updated lane:{args.name} skills: {', '.join(lane['skills']) or '(none)'}")
        return 0
    lane = cli.add_lane(root, args.name, args.paths, skills=args.skills, executor=args.executor, model=args.model,
                        title=args.title, emoji=args.emoji)
    print(f"Created lane:{args.name} ({', '.join(lane['paths'])})")
    cli._open_lane_view(root)
    return 0

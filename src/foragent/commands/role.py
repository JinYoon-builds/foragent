"""`fa role`: add, change or remove a standing role (#769)."""

from foragent.commands import facade

ORDER = 95


def add(commands) -> None:
    cli = facade()
    role = commands.add_parser("role", help="Manage standing roles (internal; the orchestrator uses it)")
    actions = role.add_subparsers(dest="action", required=True)
    for verb in ("add", "set"):
        parser = actions.add_parser(verb)
        parser.add_argument("name")
        parser.add_argument("--skill", action="append", dest="skills",
                            help="Role skill; repeat for several (set replaces the list)")
        parser.add_argument("--scope", action="append", help="Path glob the role owns; repeatable")
        parser.add_argument("--signal", action="append", dest="signals",
                            help="paths:<glob> or kinds:<fact kind>; repeatable")
        parser.add_argument("--idle-days", type=int)
        parser.add_argument("--why", help="Evidence for the change, recorded in the orchestrator events")
        parser.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    from foragent import roles

    root = args.repo_root.resolve()
    if args.action == "remove":
        roles.remove_role(root, args.name)
        print(f"Removed role {args.name}")
        return 0
    if args.action == "add":
        entry = roles.add_role(root, args.name, skills=args.skills or [], scope=args.scope or [],
                               signals=args.signals or [], why=args.why, idle_days=args.idle_days)
        print(f"Added role {args.name} (skills: {', '.join(entry['skills']) or '(none)'})")
        return 0
    entry = roles.set_role(root, args.name, skills=args.skills, scope=args.scope, signals=args.signals,
                           why=args.why, idle_days=args.idle_days)
    print(f"Updated role {args.name} (skills: {', '.join(entry.get('skills', [])) or '(none)'})")
    return 0

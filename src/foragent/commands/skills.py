"""`fa skills`: manage skill libraries."""

from foragent.commands import facade

ORDER = 100


def add(commands) -> None:
    cli = facade()
    skills = commands.add_parser("skills", help="Manage skill libraries")
    skill_actions = skills.add_subparsers(dest="action", required=True)
    skill_actions.add_parser("sync")
    add = skill_actions.add_parser("add", help="Preview a skill pack and register it after approval")
    add.add_argument("source", help="Git repository URL or local repository path")
    add.add_argument("--json", action="store_true")
    add.add_argument("--approve", action="store_true", help="Register the pack with the roles a person confirmed")
    add.add_argument("--role", action="append", default=[], metavar="SKILL=ROLE",
                     help="Confirmed role: style, review, tool, or domain; repeat for each skill")
    catalog = skill_actions.add_parser("catalog")
    catalog.add_argument("--json", action="store_true")
    prefer = skill_actions.add_parser(
        "prefer", help="Record a confirmed skill proposal as project-preferred; lane agents see it first")
    prefer.add_argument("id")
    prefer.add_argument("--repo-root", type=cli.Path, required=True)
    install = skill_actions.add_parser("install")
    install.add_argument("id")
    install.add_argument("--repo-root", type=cli.Path, required=True)
    install.add_argument("--commit", action="store_true", help="Install for committing; requires an open-source license")


def run(args) -> int:
    from foragent.skills import with_vendored_library

    cli = facade()
    # The catalog advertises the current project's copy; installing elsewhere keeps that source.
    roots = [cli.Path.cwd()] + ([args.repo_root.resolve()] if getattr(args, "repo_root", None) else [])
    from dataclasses import replace

    # The project's preferences keep their sources in the catalogue, as the lane catalogue sees them (#1653).
    preferred = _project_preferred(roots[-1])
    manager = cli.SkillManager(with_vendored_library(
        replace(cli.load_global_config(), preferred_skills=preferred), *roots))
    if args.action == "prefer":
        from foragent.config import add_preferred_skill

        if not any(skill.id == args.id for skill in manager.skills(available_only=True)):
            raise cli.SkillError(f"unknown skill: {args.id}; see fa skills catalog")
        preferred = add_preferred_skill(args.repo_root.resolve(), args.id)
        print("Preferred skills: " + ", ".join(preferred))
    elif args.action == "sync":
        print("Synced: " + ", ".join(manager.sync()))
    elif args.action in {"catalog", "add"}:
        payload = (manager.add(args.source, approve=args.approve, roles=args.role)
                   if args.action == "add" else _preferred_first(manager.catalog(), cli.Path.cwd()))
        catalog = payload["skills"] if args.action == "add" else payload
        if args.json:
            print(cli.json.dumps(payload, ensure_ascii=False))
        else:
            if args.action == "add":
                print(("Registered: " if args.approve else "Preview: ") + args.source)
            print("ID\tName\tLicense\tDescription\tRole\tRuns scripts\tCopies")
            for skill in catalog:
                description = " ".join(skill["description"].split())
                print(f"{skill['id']}\t{skill['name']}\t{skill['license']}\t{description}\t"
                      f"{skill['role'] or 'unassigned'}\t{'yes' if skill['runs_scripts'] else 'no'}\t"
                      + ", ".join(skill["copies"]))
            for item in payload.get("skipped", []) if args.action == "add" else []:
                print(f"Skipped: {item['path']}: {item['error']}")
    else:
        print("Installed: " + manager.install(args.id, args.repo_root.resolve(), commit=args.commit))
    return 0


def _project_preferred(root) -> list[str]:
    from foragent.config import ConfigError, load_config

    try:
        return load_config(root).preferred_skills if (root / ".foragent.yml").is_file() else []
    except ConfigError:
        return []


def _preferred_first(catalog: list[dict], root) -> list[dict]:
    """The project's preferred skills first, in the order a person confirmed them; the rest keep their order."""
    preferred = _project_preferred(root)
    rank = {identity: index for index, identity in enumerate(preferred)}
    return sorted(catalog, key=lambda skill: rank.get(skill["id"], len(rank)))

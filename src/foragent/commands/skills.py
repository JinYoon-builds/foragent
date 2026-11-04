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
    from dataclasses import replace
    from foragent.skills import SkillManager, catalog_skills, project_skill_config, with_vendored_library

    cli = facade()
    # Explicit installation keeps the current project's vendored copy as its source.
    roots = [cli.Path.cwd()] + ([args.repo_root.resolve()] if getattr(args, "repo_root", None) else [])
    if args.action in {"catalog", "prefer"}:
        config = project_skill_config(roots[-1], global_config=cli.load_global_config())
        skills = catalog_skills(config, required=True, manager_factory=cli.SkillManager)
    else:
        # Keep confirmed shipped skills installable even after the default libraries are removed.
        preferred = project_skill_config(roots[-1]).preferred_skills
        manager = cli.SkillManager(with_vendored_library(
            replace(cli.load_global_config(), preferred_skills=preferred), *roots))
    if args.action == "prefer":
        from foragent.config import add_preferred_skill

        if not any(skill.id == args.id for skill in skills):
            raise cli.SkillError(f"unknown skill: {args.id}; see fa skills catalog")
        preferred = add_preferred_skill(args.repo_root.resolve(), args.id)
        print("Preferred skills: " + ", ".join(preferred))
    elif args.action == "sync":
        print("Synced: " + ", ".join(manager.sync()))
    elif args.action in {"catalog", "add"}:
        payload = (manager.add(args.source, approve=args.approve, roles=args.role)
                   if args.action == "add" else [SkillManager._summary(skill) for skill in skills])
        catalog = payload["skills"] if args.action == "add" else payload
        if args.json:
            print(cli.json.dumps(payload, ensure_ascii=False))
        else:
            if args.action == "add":
                print(("Registered: " if args.approve else "Preview: ") + args.source)
            print("ID\tName\tLicense\tSource\tDescription\tRole\tRuns scripts\tCopies")
            for skill in catalog:
                description = " ".join(skill["description"].split())
                print(f"{skill['id']}\t{skill['name']}\t{skill['license']}\t{skill['source']}\t{description}\t"
                      f"{skill['role'] or 'unassigned'}\t{'yes' if skill['runs_scripts'] else 'no'}\t"
                      + ", ".join(skill["copies"]))
            for item in payload.get("skipped", []) if args.action == "add" else []:
                print(f"Skipped: {item['path']}: {item['error']}")
    else:
        print("Installed: " + manager.install(args.id, args.repo_root.resolve(), commit=args.commit))
    return 0

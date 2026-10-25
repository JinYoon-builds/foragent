"""`fa skills`: manage skill libraries."""

import json
from pathlib import Path

from foragent.commands import facade

ORDER = 100


def add(commands) -> None:
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
    install = skill_actions.add_parser("install")
    install.add_argument("id")
    install.add_argument("--repo-root", type=Path, required=True)
    install.add_argument("--commit", action="store_true", help="Install for committing; requires an open-source license")


def run(args) -> int:
    cli = facade()
    manager = cli.SkillManager(cli.load_global_config())
    if args.action == "sync":
        print("Synced: " + ", ".join(manager.sync()))
    elif args.action in {"catalog", "add"}:
        payload = (manager.add(args.source, approve=args.approve, roles=args.role)
                   if args.action == "add" else manager.catalog())
        catalog = payload["skills"] if args.action == "add" else payload
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
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

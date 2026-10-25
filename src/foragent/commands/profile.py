"""`fa profile set`: save the project's confirmed profile."""

import json
from pathlib import Path

from foragent.commands import facade

ORDER = 70


def add(commands) -> None:
    profile_command = commands.add_parser("profile", help="Save the project's confirmed profile")
    profile_actions = profile_command.add_subparsers(dest="action", required=True)
    profile_set = profile_actions.add_parser("set")
    for field in ("kind", "stage", "priority"):
        profile_set.add_argument(f"--{field}", required=True)
    profile_set.add_argument("--repo-root", type=Path, default=Path.cwd())


def run(args) -> int:
    root = args.repo_root.resolve()
    print(json.dumps(facade().set_profile(root, kind=args.kind, stage=args.stage, priority=args.priority),
                     ensure_ascii=False))
    return 0

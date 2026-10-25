"""`fa brief`: the start-of-session facts for planning."""

import json
from pathlib import Path

from foragent.commands import facade, repo_store

ORDER = 150


def add(commands) -> None:
    briefing = commands.add_parser("brief", help="Show the start-of-session facts for planning")
    briefing.add_argument("--json", action="store_true")
    briefing.add_argument("--repo-root", type=Path, default=Path.cwd())


def run(args) -> int:
    config, _repo, store = repo_store(args.repo_root.resolve())
    facts = facade().brief.collect(store)
    print(json.dumps(facts, ensure_ascii=False) if args.json
          else facade().brief.render(facts, config.language))
    return 0

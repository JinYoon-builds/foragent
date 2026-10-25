"""`fa order`: deterministic tools for fa-order."""

import argparse
import json
from pathlib import Path

from foragent.commands import facade, repo_store

ORDER = 80

PUBLISH_EPILOG = '''PLAN.json schema (unknown fields are rejected):
{
  "id": "2026-10-20-login", "title": "Login MVP", "summary": "Goal of this order",
  "spec_markdown": "# Login\\n...",
  "new_lanes": [{"name": "api", "paths": ["api/**"], "skills": []}],
  "issues": [{"key": "login", "title": "Login API", "goal": "...", "out_of_scope": "...",
    "done_when": ["uv run pytest tests/acceptance/test_login.py"], "checklist": [],
    "lane": "api", "size": "m", "interactive": false, "blocked_by": [], "files": ["src/api/**"],
    "acceptance_files": {"tests/acceptance/test_login.py": "..."}}],
  "edits": [{"number": 3, "goal": "...", "out_of_scope": "...", "done_when": ["true"],
    "checklist": [], "files": [], "acceptance_files": {}}]
}
new_lanes, edits, checklist, blocked_by, files, and acceptance_files default to empty collections.
issues[].interactive is an optional boolean (default false); true adds the interactive label.
--approve (or --approve-keys) needs a clean `fa order preread` of each approved issue's current
content; --dry-run never needs one.
files lists repository-relative paths or globs expected to change; absolute paths and .. are rejected.'''


def _comma_keys(value: str) -> list[str]:
    values = value.split(",")
    if any(not item.strip() for item in values):
        raise argparse.ArgumentTypeError("expected comma-separated keys")
    return [item.strip() for item in values]


def _comma_numbers(value: str) -> list[int]:
    try:
        numbers = [int(item) for item in facade()._comma_keys(value)]
        if any(number <= 0 for number in numbers):
            raise ValueError
        return numbers
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated positive issue numbers") from exc


def add(commands) -> None:
    cli = facade()
    order_command = commands.add_parser("order", help="Deterministic tools for fa-order")
    order_actions = order_command.add_subparsers(dest="action", required=True)
    context = order_actions.add_parser("context")
    context.add_argument("--repo-root", type=Path, default=Path.cwd())
    context.add_argument("--json", action="store_true")
    progress = order_actions.add_parser("progress", help="Record local order preparation facts")
    progress_steps = progress.add_subparsers(dest="step", required=True)
    for step in cli.order.PROGRESS_STEPS:
        command = progress_steps.add_parser(step)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        if step == "start":
            command.add_argument("--title", required=True)
        elif step in {"ledger", "chunk"}:
            command.add_argument("--add", required=True)
        elif step == "cell":
            command.add_argument("--chunk", required=True)
            command.add_argument("--slot", choices=cli.order.PROGRESS_SLOTS, required=True)
            cell = command.add_mutually_exclusive_group(required=True)
            cell.add_argument("--ask")
            cell.add_argument("--filled", action="store_true")
        elif step == "tests":
            command.add_argument("--done", type=int, required=True)
            command.add_argument("--total", type=int, required=True)
        elif step == "publish":
            command.add_argument("--issue", type=int, required=True)
    preread = order_actions.add_parser(
        "preread", help="Ask each issue's implementer, read-only, to list unknowns before approval")
    preread.add_argument("plan", type=Path)
    preread.add_argument("--repo-root", type=Path, default=Path.cwd())
    preread.add_argument("--json", action="store_true")
    publish = order_actions.add_parser(
        "publish", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=PUBLISH_EPILOG)
    publish.add_argument("plan", type=Path)
    publish.add_argument("--repo-root", type=Path, default=Path.cwd())
    publish.add_argument("--approve", action="store_true")
    publish.add_argument("--approve-keys", type=cli._comma_keys, default=[])
    publish.add_argument("--approve-numbers", type=cli._comma_numbers, default=[])
    publish.add_argument("--dry-run", action="store_true")


def _preread(args, root: Path) -> int:
    cli = facade()
    config = cli.load_config(root)
    results = cli.order.preread(cli.order.read_plan(args.plan), root, config,
                                lambda: cli.GhTracker(config.repo or cli.GitRepo(root).remote_slug()))
    if args.json:
        print(json.dumps({"issues": results}, ensure_ascii=False))
    else:
        for result in results:
            print(f"{result['key']}: " + ("no unknowns" if not result["unknowns"] else
                                          f"{len(result['unknowns'])} unknown(s)"))
            for unknown in result["unknowns"]:
                print(f"  - {unknown}")
    return 0


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if args.action == "progress":
        _config, _repo, store = repo_store(root)
        fields = {name: getattr(args, name) for name in
                  ("title", "add", "chunk", "slot", "ask", "filled", "done", "total", "issue")
                  if hasattr(args, name)}
        print(json.dumps(cli.order.progress(store, args.step, **fields), ensure_ascii=False))
        return 0
    if args.action == "preread":
        return _preread(args, root)
    git = cli.GitRepo(root)
    config = cli.load_config(root)
    repo = config.repo or git.remote_slug()
    tracker = cli.GhTracker(repo)
    if args.action == "context":
        print(json.dumps(cli.order.context(root, config, tracker), ensure_ascii=False))
        return 0
    tracker = cli.RunContext(repo=repo, repo_root=root, tracker=tracker,
                             store=cli.StateStore.for_repo(repo), config=config, git=git,
                             now=lambda: cli.datetime.now(cli.UTC)).tracker
    plan = cli.order.read_plan(args.plan)
    print(cli.order.publish(
        plan, root, config, tracker, repo=repo, git=git,
        approve=args.approve, approve_keys=args.approve_keys,
        approve_numbers=args.approve_numbers, dry_run=args.dry_run,
    ).summary())
    if not args.dry_run:
        cli._open_lane_view(root)
    return 0

"""`fa plan heartbeat`: planning-session signals for the orchestrator."""

from pathlib import Path

from foragent.commands import facade, repo_store

ORDER = 130


def add(commands) -> None:
    plan_command = commands.add_parser("plan", help="Planning-session signals for the orchestrator")
    plan_actions = plan_command.add_subparsers(dest="action", required=True)
    heartbeat = plan_actions.add_parser(
        "heartbeat", help="Mark the planning session alive; questions reach it by message, not push")
    heartbeat.add_argument("--repo-root", type=Path, default=Path.cwd())


def run(args) -> int:
    _config, _repo, store = repo_store(args.repo_root.resolve())
    record = facade().orch_inbox.write_plan_heartbeat(store)
    print(f"Planning session alive at {record['at']}.")
    return 0

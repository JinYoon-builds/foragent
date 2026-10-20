"""Command-line entry points for the issue gate."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from foragent import __version__
from foragent.config import ConfigError, load_config
from foragent.git import GitError, GitRepo
from foragent.labels import APPROVED, NEEDS_HUMAN, READY, RUNNING
from foragent.launcher import spawn_runner
from foragent.reasons import parse_reason
from foragent.scheduler import TickContext, tick
from foragent.runner import RunContext, run_issue
from foragent.state import StateStore
from foragent.tracker import GhTracker, Tracker, TrackerError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fa")
    parser.add_argument("--version", action="version", version=f"foragent {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate", help="Schedule and inspect approved issues")
    actions = gate.add_subparsers(dest="action", required=True)
    for name in ("tick", "status"):
        command = actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
        if name == "tick":
            command.add_argument("--no-start", action="store_true")
    run_issue = actions.add_parser("run-issue", help="Run one approved issue")
    run_issue.add_argument("number", type=int)
    run_issue.add_argument("--repo-root", type=Path, required=True)
    return parser


def _status(tracker: Tracker, store: StateStore) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {
        state: [] for state in (NEEDS_HUMAN, RUNNING, "pr-open", READY, "approved-waiting", "unapproved")
    }
    for issue in sorted(tracker.list_open_issues(), key=lambda issue: issue.number):
        item: dict[str, Any] = {"number": issue.number, "title": issue.title}
        if NEEDS_HUMAN in issue.labels:
            group = NEEDS_HUMAN
            comments = sorted(
                tracker.list_comments(issue.number),
                key=lambda comment: (datetime.fromisoformat(comment.created_at), comment.id),
            )
            item["reason"] = next(
                (reason.value for comment in reversed(comments)
                 if (reason := parse_reason(comment.body)) is not None), None,
            )
        elif RUNNING in issue.labels:
            group = RUNNING
        elif APPROVED in issue.labels and tracker.find_pr(f"foragent/issue-{issue.number}") is not None:
            group = "pr-open"
        elif READY in issue.labels:
            group = READY
        elif APPROVED in issue.labels:
            group = "approved-waiting"
        else:
            group = "unapproved"
        groups[group].append(item)
    return {
        "issues": groups,
        "active_runs": [asdict(run) for run in store.active_runs()],
        "history": [asdict(run) for run in store.read_history()[-10:]],
    }


def _print_status(status: dict[str, Any]) -> None:
    for state, issues in status["issues"].items():
        print(f"{state}:")
        for issue in issues:
            reason = f" [{issue['reason']}]" if issue.get("reason") else ""
            print(f"  #{issue['number']} {issue['title']}{reason}")
    print("Active runs:")
    for run in status["active_runs"]:
        print(f"  #{run['number']} {run['branch']} pid={run['pid']}")
    print("History (last 10):")
    for run in status["history"]:
        print(f"  #{run['number']} {run['outcome']} started={run['started_at']}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.repo_root.resolve()
        git = GitRepo(root)
        config = load_config(root)
        repo = config.repo or git.remote_slug()
        tracker = GhTracker(repo)
        store = StateStore.for_repo(repo)
        if args.action == "tick":
            report = tick(TickContext(
                repo=repo, repo_root=root, tracker=tracker, store=store, config=config,
                git=git, now=lambda: datetime.now(UTC), launcher=spawn_runner(root, store),
                start_runs=not args.no_start,
            ))
            if args.json:
                print(json.dumps(asdict(report)))
            else:
                if report.skipped:
                    print(report.skipped)
                for action in report.actions:
                    print(action)
        elif args.action == "status":
            status = _status(tracker, store)
            if args.json:
                print(json.dumps(status))
            else:
                _print_status(status)
        else:
            if store.get_run(args.number) is None:
                raise ConfigError(f"no run record for issue #{args.number}")
            store.update_run(args.number, pid=os.getpid())
            outcome = run_issue(args.number, RunContext(
                repo=repo, repo_root=root, tracker=tracker, store=store,
                config=config, git=git, now=lambda: datetime.now(UTC),
            ))
            return 1 if outcome == "error" else 0
        return 0
    except (ConfigError, TrackerError, GitError, OSError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

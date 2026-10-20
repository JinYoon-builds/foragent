"""Command-line entry points for the issue gate."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from foragent import __version__, inspect_actions, inspect_data, launchd
from foragent.config import ConfigError, load_config
from foragent.git import GitError, GitRepo
from foragent.init import init_repo
from foragent.labels import APPROVED, NEEDS_HUMAN, READY, RUNNING
from foragent.launcher import spawn_notify, spawn_runner
from foragent.reasons import parse_reason
from foragent.inspect_runtime import InspectRuntimeError
from foragent.scheduler import TickContext, tick
from foragent.runner import RunContext, check_done, run_issue
from foragent.state import StateStore
from foragent.tracker import GhTracker, Tracker, TrackerError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fa")
    parser.add_argument("--version", action="version", version=f"foragent {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Set up this repository for Foragent")
    init.add_argument("--repo-root", type=Path, default=Path.cwd())
    init.add_argument("--approver", nargs="+", action="extend", dest="approvers")
    init.add_argument("--review", choices=("fast", "batch", "careful"))
    init.add_argument("--executor", choices=("codex", "claude"))
    init.add_argument("--run-hours")
    init.add_argument("--language")
    init.add_argument("--no-push", action="store_true")
    init.add_argument("--no-launchd", action="store_true")
    init.add_argument("--dry-run", action="store_true")
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
    done = actions.add_parser("check-done", help="Check Done when for a native Stop hook")
    done.add_argument("--snapshot", type=Path, required=True)
    done.add_argument("--worktree", type=Path, required=True)
    done.add_argument("--acceptance-dir", required=True)
    for name in ("install", "uninstall", "launchd-status"):
        command = actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
    inspect = commands.add_parser("inspect", help="Show the human inbox and record answers")
    inspect.add_argument("--repo-root", type=Path, default=Path.cwd())
    inspect.add_argument("--json", action="store_true")
    answers = inspect.add_subparsers(dest="action")
    for name in ("answer", "retry", "feedback", "mark-seen", "notify", "prepare", "serve", "stop"):
        command = answers.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
        if name in {"answer", "retry", "serve"}:
            command.add_argument("number", type=int)
        if name == "answer":
            command.add_argument("--text", required=True)
        if name == "feedback":
            command.add_argument("--title", required=True)
            command.add_argument("--body", required=True)
            command.add_argument("--about", type=int)
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
    print(f"launchd: {'loaded' if status['launchd']['loaded'] else 'not loaded'}")
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


def _inspect(args: argparse.Namespace, ctx: inspect_data.InspectContext) -> None:
    if args.action == "prepare":
        numbers = inspect_actions.prepare(ctx)
        print("Prepared: " + ", ".join(f"#{number}" for number in numbers) if numbers else "No new results to prepare.")
    elif args.action == "serve":
        for url in inspect_actions.serve(ctx, args.number):
            print(url)
    elif args.action == "stop":
        inspect_actions.stop(ctx)
        print("Served apps stopped.")
    elif args.action == "answer":
        comment = inspect_actions.answer(ctx, args.number, args.text)
        print(f"Decision recorded: https://github.com/{ctx.repo}/issues/{args.number}#issuecomment-{comment}")
    elif args.action == "retry":
        inspect_actions.retry(ctx, args.number)
        print(f"Retry requested for #{args.number}.")
    elif args.action == "feedback":
        number = inspect_actions.feedback(ctx, args.title, args.body, args.about)
        print(f"Feedback: https://github.com/{ctx.repo}/issues/{number}")
    elif args.action == "mark-seen":
        print(f"Seen through {inspect_actions.mark_seen(ctx)}")
    elif args.action == "notify":
        if line := inspect_actions.notify(ctx):
            print(line)
    else:
        inbox = inspect_data.collect(ctx)
        if args.json:
            print(json.dumps(asdict(inbox)))
        else:
            print(inspect_data.counts_line(inbox, detailed=True))
            for item in inbox.items:
                summary = f"[{item.group}] #{item.number} {item.title} — {item.summary}"
                if item.group == "try":
                    print(f"{summary} {item.url}")
                    for line in inspect_data.try_lines(item):
                        print("  " + line.replace("\n", "\n  "))
                    continue
                print(summary)
                for line in item.details.splitlines()[:12]:
                    print(f"  {line}")
                print(f"  {item.url}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "gate" and args.action == "check-done":
            return check_done(args.snapshot.resolve(), args.worktree.resolve(), args.acceptance_dir)
        root = args.repo_root.resolve()
        if args.command == "init":
            print(init_repo(
                root, approvers=args.approvers, review=args.review, executor=args.executor,
                run_hours=args.run_hours, no_push=args.no_push, no_launchd=args.no_launchd,
                dry_run=args.dry_run, language=args.language,
            ).summary())
            return 0
        if args.action in {"install", "uninstall", "launchd-status"}:
            if args.action == "launchd-status":
                report = launchd.status(root)
                print(f"{report.label}: plist {'present' if report.plist_present else 'missing'}, "
                      f"{'loaded' if report.loaded else 'not loaded'}, "
                      f"last exit status: {report.last_exit_status if report.last_exit_status is not None else 'unknown'}")
            else:
                label = getattr(launchd, args.action)(root)
                print(f"launchd {'installed' if args.action == 'install' else 'uninstalled'}: {label}")
            return 0
        git = GitRepo(root)
        config = load_config(root)
        repo = config.repo or git.remote_slug()
        tracker = GhTracker(repo)
        store = StateStore.for_repo(repo)
        inspect_ctx = inspect_data.InspectContext(
            tracker, store, repo, config, lambda: datetime.now(UTC), repo_root=root, git=git,
        )
        if args.command == "inspect":
            _inspect(args, inspect_ctx)
            return 0
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
            try:
                spawn_notify(root, store)
            except Exception as exc:
                print(f"foragent: inspect notify failed: {' '.join(str(exc).split())}", file=sys.stderr)
        elif args.action == "status":
            status = _status(tracker, store)
            status["launchd"] = asdict(launchd.status(root, repo=repo))
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
    except (ConfigError, TrackerError, GitError, launchd.LaunchdError, OSError,
            inspect_actions.InspectError, InspectRuntimeError, subprocess.SubprocessError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

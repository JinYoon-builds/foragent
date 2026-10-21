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

from foragent import __version__, adopt, bench, design, inspect_actions, inspect_data, launchd, order
from foragent.config import Config, ConfigError, load_config, load_global_config
from foragent.git import GitError, GitRepo
from foragent.init import init_repo
from foragent.kick import kick_tick
from foragent.labels import APPROVED, NEEDS_HUMAN, READY, RUNNING, issue_lane, lane_order, state_group
from foragent.lanes import LaneError, add_lane
from foragent.paths import PathError
from foragent.skills import SkillError, SkillManager
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
    design_command = commands.add_parser("design", help="Check the design foundation")
    design_actions = design_command.add_subparsers(dest="action", required=True)
    lint = design_actions.add_parser("lint", help="Report raw colors outside token files")
    lint.add_argument("--tokens", nargs="+", action="extend", help="Token file paths or globs (default: **/tokens.css **/tokens.ts)")
    lint.add_argument("--glob", nargs="+", action="extend", dest="globs", help="UI file globs (default: CSS, TSX, JSX, Vue, Svelte, HTML)")
    lint.add_argument("--json", action="store_true")
    adopt_command = commands.add_parser("adopt", help="Prepare an existing repository for Foragent")
    adopt_actions = adopt_command.add_subparsers(dest="action", required=True)
    for name in ("survey", "baseline", "ci"):
        command = adopt_actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
        if name == "baseline":
            command.add_argument("--command", dest="test_command")
            command.add_argument("--timeout", type=int, default=900)
        elif name == "ci":
            command.add_argument("--exclude-acceptance", action="store_true")
    bench_command = commands.add_parser("bench", help="Record and report benchmark scenarios")
    bench_actions = bench_command.add_subparsers(dest="action", required=True)
    record = bench_actions.add_parser("record")
    record.add_argument("scenario_dir", type=Path)
    record.add_argument("--arm", choices=("A", "B", "C"), required=True)
    record.add_argument("--repo", type=Path, required=True)
    record.add_argument("--started", required=True)
    record.add_argument("--finished")
    record.add_argument("--state-dir", type=Path)
    record.add_argument("--tokens", type=int)
    record.add_argument("--interventions", type=int)
    report = bench_actions.add_parser("report")
    report.add_argument("scenario_dir", type=Path)
    report.add_argument("--json", action="store_true")
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
    order_command = commands.add_parser("order", help="Deterministic tools for fa-order")
    order_actions = order_command.add_subparsers(dest="action", required=True)
    context = order_actions.add_parser("context")
    context.add_argument("--repo-root", type=Path, default=Path.cwd())
    context.add_argument("--json", action="store_true")
    publish = order_actions.add_parser(
        "publish", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''PLAN.json schema (unknown fields are rejected):
{
  "id": "2026-10-20-login", "title": "Login MVP", "summary": "Goal of this order",
  "spec_markdown": "# Login\\n...",
  "new_lanes": [{"name": "api", "paths": ["api/**"], "skills": []}],
  "issues": [{"key": "login", "title": "Login API", "goal": "...", "out_of_scope": "...",
    "done_when": ["uv run pytest tests/acceptance/test_login.py"], "checklist": [],
    "lane": "api", "size": "m", "blocked_by": [],
    "acceptance_files": {"tests/acceptance/test_login.py": "..."}}],
  "edits": [{"number": 3, "goal": "...", "out_of_scope": "...", "done_when": ["true"],
    "checklist": [], "acceptance_files": {}}]
}
new_lanes, edits, checklist, blocked_by, and acceptance_files default to empty collections.''',
    )
    publish.add_argument("plan", type=Path)
    publish.add_argument("--repo-root", type=Path, default=Path.cwd())
    publish.add_argument("--approve", action="store_true")
    publish.add_argument("--approve-keys", type=_comma_keys, default=[])
    publish.add_argument("--approve-numbers", type=_comma_numbers, default=[])
    publish.add_argument("--dry-run", action="store_true")
    lanes = commands.add_parser("lanes", help="Create repository lanes")
    lane_actions = lanes.add_subparsers(dest="action", required=True)
    lane = lane_actions.add_parser("add")
    lane.add_argument("name")
    lane.add_argument("--paths", nargs="+", required=True)
    lane.add_argument("--skill", nargs="+", action="extend", default=[], dest="skills")
    lane.add_argument("--executor", choices=("codex", "claude"))
    lane.add_argument("--model")
    lane.add_argument("--repo-root", type=Path, default=Path.cwd())
    skills = commands.add_parser("skills", help="Manage skill libraries")
    skill_actions = skills.add_subparsers(dest="action", required=True)
    skill_actions.add_parser("sync")
    catalog = skill_actions.add_parser("catalog")
    catalog.add_argument("--json", action="store_true")
    install = skill_actions.add_parser("install")
    install.add_argument("id")
    install.add_argument("--repo-root", type=Path, required=True)
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


def _comma_keys(value: str) -> list[str]:
    values = value.split(",")
    if any(not item.strip() for item in values):
        raise argparse.ArgumentTypeError("expected comma-separated keys")
    return [item.strip() for item in values]


def _comma_numbers(value: str) -> list[int]:
    try:
        numbers = [int(item) for item in _comma_keys(value)]
        if any(number <= 0 for number in numbers):
            raise ValueError
        return numbers
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated positive issue numbers") from exc


def _status(tracker: Tracker, store: StateStore, config: Config | None = None) -> dict[str, Any]:
    config = config or Config()
    lanes: dict[str, dict[str, list]] = {}
    groups: dict[str, list[dict[str, Any]]] = {
        state: [] for state in (NEEDS_HUMAN, RUNNING, "pr-open", READY, "approved-waiting", "unapproved")
    }
    for issue in sorted(tracker.list_open_issues(), key=lambda issue: issue.number):
        item: dict[str, Any] = {"number": issue.number, "title": issue.title}
        group = state_group(issue, tracker)
        if group == NEEDS_HUMAN:
            comments = sorted(
                tracker.list_comments(issue.number),
                key=lambda comment: (datetime.fromisoformat(comment.created_at), comment.id),
            )
            item["reason"] = next(
                (reason.value for comment in reversed(comments)
                 if (reason := parse_reason(comment.body)) is not None), None,
            )
        groups[group].append(item)
        if group in {RUNNING, READY}:
            run = store.get_run(issue.number) if group == RUNNING else None
            lane = run.lane if run is not None and run.status == "running" else issue_lane(issue.labels, config.lanes)
            lanes.setdefault(lane, {RUNNING: [], READY: []})[group].append(item)
    return {
        "issues": groups,
        "lanes": {lane: lanes[lane] for lane in sorted(lanes, key=lane_order)},
        "active_runs": [asdict(run) for run in store.active_runs()],
        "history": [asdict(run) for run in store.read_history()[-10:]],
    }


def _print_status(status: dict[str, Any]) -> None:
    print(f"launchd: {'loaded' if status['launchd']['loaded'] else 'not loaded'}")
    for state, issues in status["issues"].items():
        print(f"{state}:")
        if state in {RUNNING, READY}:
            for lane, grouped in status["lanes"].items():
                if grouped[state]:
                    print(f"  lane:{lane}:")
                    for issue in grouped[state]:
                        print(f"    #{issue['number']} {issue['title']}")
            continue
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
        kick_tick(ctx.repo_root)
        print(f"Decision recorded: https://github.com/{ctx.repo}/issues/{args.number}#issuecomment-{comment}")
    elif args.action == "retry":
        inspect_actions.retry(ctx, args.number)
        kick_tick(ctx.repo_root)
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
            print(json.dumps(inspect_data.inbox_payload(inbox)))
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
        if args.command == "design":
            report = design.lint(Path.cwd(), args.tokens, args.globs)
            if args.json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                for item in report["violations"]:
                    print(f"{item['file']}:{item['line']}:{item['column']}: raw color {item['literal']}")
                print(f"{len(report['violations'])} raw color literal(s) found.")
            return 1 if report["violations"] else 0
        if args.command == "adopt":
            return adopt.run(args)
        if args.command == "bench":
            return bench.run(args)
        if args.command == "gate" and args.action == "check-done":
            return check_done(args.snapshot.resolve(), args.worktree.resolve(), args.acceptance_dir)
        if args.command == "skills":
            manager = SkillManager(load_global_config())
            if args.action == "sync":
                print("Synced: " + ", ".join(manager.sync()))
            elif args.action == "catalog":
                catalog = manager.catalog()
                if args.json:
                    print(json.dumps(catalog, ensure_ascii=False))
                else:
                    print("ID\tName\tLicense\tDescription")
                    for skill in catalog:
                        description = " ".join(skill["description"].split())
                        print(f"{skill['id']}\t{skill['name']}\t{skill['license']}\t{description}")
            else:
                print("Installed: " + manager.install(args.id, args.repo_root.resolve()))
            return 0
        root = args.repo_root.resolve()
        if args.command == "init":
            print(init_repo(
                root, approvers=args.approvers, review=args.review, executor=args.executor,
                run_hours=args.run_hours, no_push=args.no_push, no_launchd=args.no_launchd,
                dry_run=args.dry_run, language=args.language,
            ).summary())
            return 0
        if args.command == "lanes":
            lane = add_lane(root, args.name, args.paths, skills=args.skills, executor=args.executor, model=args.model)
            print(f"Created lane:{args.name} ({', '.join(lane['paths'])})")
            return 0
        if args.action in {"install", "uninstall", "launchd-status"}:
            if args.action == "launchd-status":
                report = launchd.status(root)
                print(f"{report.label}: plist {'present' if report.plist_present else 'missing'}, "
                      f"{'loaded' if report.loaded else 'not loaded'}, "
                      f"last exit status: {report.last_exit_status if report.last_exit_status is not None else 'unknown'}")
            else:
                label = getattr(launchd, args.action)(root)
                if args.action == "install":
                    kick_tick(root)
                print(f"launchd {'installed' if args.action == 'install' else 'uninstalled'}: {label}")
            return 0
        git = GitRepo(root)
        config = load_config(root)
        repo = config.repo or git.remote_slug()
        tracker = GhTracker(repo)
        if args.command == "order":
            if args.action == "context":
                print(json.dumps(order.context(root, config, tracker), ensure_ascii=False))
            else:
                plan = order.read_plan(args.plan)
                print(order.publish(
                    plan, root, config, tracker, repo=repo, git=git,
                    approve=args.approve, approve_keys=args.approve_keys,
                    approve_numbers=args.approve_numbers, dry_run=args.dry_run,
                ).summary())
            return 0
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
            status = _status(tracker, store, config)
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
            inspect_actions.InspectError, InspectRuntimeError, subprocess.SubprocessError,
            order.PlanError, LaneError, SkillError, PathError, json.JSONDecodeError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

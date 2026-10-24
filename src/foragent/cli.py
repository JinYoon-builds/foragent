"""Command-line entry points for the issue gate."""

import argparse
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from foragent import __version__, adopt, bench, brief, capacity, design, design_session, inspect_actions, inspect_data, launchd, notify, orch, orch_inbox, order, tidy, watch, watch_open
from foragent.config import Config, ConfigError, load_config, load_global_config, set_profile
from foragent.git import GitError, GitRepo
from foragent.init import init_repo
from foragent.i18n import text
from foragent.kick import kick_tick
from foragent.labels import READY, RUNNING
from foragent.lanes import LaneError, add_lane, set_lane_skills
from foragent.paths import PathError
from foragent.skills import SkillError, SkillManager
from foragent.launcher import spawn_notify, spawn_runner
from foragent.inspect_runtime import InspectRuntimeError
from foragent.scheduler import TickContext, tick
from foragent.runner import RunContext, check_done, run_issue, run_tests
from foragent.run_baseline import start_baseline_process
from foragent.state import StateStore
from foragent.status import collect as collect_status, summary_values, unknown as status_unknown
from foragent.tracker import GhTracker, RecordingTracker, Tracker, TrackerError

try:
    from foragent._build import GIT_SHA
except ImportError:
    GIT_SHA = "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fa")
    parser.add_argument("--version", action="version", version=f"foragent {__version__} ({GIT_SHA})")
    commands = parser.add_subparsers(dest="command", required=True)
    design_command = commands.add_parser("design", help="Preview designs and check the project's foundation")
    design_actions = design_command.add_subparsers(dest="action", required=True,
                                                  metavar="{lint,preview,picks,done}")
    lint = design_actions.add_parser("lint", help="Report raw colors outside token files")
    lint.add_argument("--repo-root", type=Path, default=Path.cwd())
    lint.add_argument("--tokens", nargs="+", action="extend", help="Token paths/globs (replaces defaults)")
    lint.add_argument("--glob", nargs="+", action="extend", dest="globs", help="UI globs (replaces defaults)")
    lint.add_argument("--json", action="store_true")
    preview = design_actions.add_parser("preview", help="Show references or running drafts in planning mode")
    preview.add_argument("--stage", choices=("board", "drafts"), default="drafts")
    preview.add_argument("--topic", default="default")
    preview.add_argument("--references", type=Path, help="JSON reference manifest for the local board")
    medium = preview.add_mutually_exclusive_group()
    medium.add_argument("--command", dest="preview_commands", action="append", help="Runnable terminal draft; repeat to compare")
    medium.add_argument("--url", action="append", help="Running web draft; repeat to compare")
    preview.add_argument("--repo-root", type=Path, default=Path.cwd())
    picks = design_actions.add_parser("picks", help="Read saved reference likes and dislikes")
    picks.add_argument("--topic", required=True)
    picks.add_argument("--json", action="store_true")
    picks.add_argument("--repo-root", type=Path, default=Path.cwd())
    done = design_actions.add_parser("done", help="Close preview panes and return to the lane view")
    done.add_argument("--repo-root", type=Path, default=Path.cwd())
    serve = design_actions.add_parser("_serve")
    serve.add_argument("--directory", type=Path, required=True)
    serve.add_argument("--topic", required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--repo-root", type=Path, default=Path.cwd())
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
    init.add_argument("--no-board", action="store_true")
    init.add_argument("--board-map", help="Existing board columns: Backlog=Todo,Up next=Todo,In progress=Doing,Your turn=Review,Done=Done")
    init.add_argument("--dry-run", action="store_true")
    profile_command = commands.add_parser("profile", help="Save the project's confirmed profile")
    profile_actions = profile_command.add_subparsers(dest="action", required=True)
    profile_set = profile_actions.add_parser("set")
    for field in ("kind", "stage", "priority"):
        profile_set.add_argument(f"--{field}", required=True)
    profile_set.add_argument("--repo-root", type=Path, default=Path.cwd())
    order_command = commands.add_parser("order", help="Deterministic tools for fa-order")
    order_actions = order_command.add_subparsers(dest="action", required=True)
    context = order_actions.add_parser("context")
    context.add_argument("--repo-root", type=Path, default=Path.cwd())
    context.add_argument("--json", action="store_true")
    progress = order_actions.add_parser("progress", help="Record local order preparation facts")
    progress_steps = progress.add_subparsers(dest="step", required=True)
    for step in order.PROGRESS_STEPS:
        command = progress_steps.add_parser(step)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        if step == "start":
            command.add_argument("--title", required=True)
        elif step in {"ledger", "chunk"}:
            command.add_argument("--add", required=True)
        elif step == "cell":
            command.add_argument("--chunk", required=True)
            command.add_argument("--slot", choices=order.PROGRESS_SLOTS, required=True)
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
        "publish", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''PLAN.json schema (unknown fields are rejected):
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
files lists repository-relative paths or globs expected to change; absolute paths and .. are rejected.''',
    )
    publish.add_argument("plan", type=Path)
    publish.add_argument("--repo-root", type=Path, default=Path.cwd())
    publish.add_argument("--approve", action="store_true")
    publish.add_argument("--approve-keys", type=_comma_keys, default=[])
    publish.add_argument("--approve-numbers", type=_comma_numbers, default=[])
    publish.add_argument("--dry-run", action="store_true")
    lanes = commands.add_parser("lanes", help="Manage repository lanes")
    lane_actions = lanes.add_subparsers(dest="action", required=True)
    lane = lane_actions.add_parser("add")
    lane.add_argument("name")
    lane.add_argument("--paths", nargs="+", required=True)
    lane.add_argument("--skill", nargs="+", action="extend", default=[], dest="skills")
    lane.add_argument("--executor", choices=("codex", "claude"))
    lane.add_argument("--model")
    lane.add_argument("--title")
    lane.add_argument("--emoji")
    lane.add_argument("--repo-root", type=Path, default=Path.cwd())
    fixed_skills = lane_actions.add_parser("skills", help="Replace the fixed skills a person chose for a lane")
    fixed_skills.add_argument("name")
    fixed_skills.add_argument("ids", nargs="*", help="Skill IDs; omit to clear the lane's fixed skills")
    fixed_skills.add_argument("--repo-root", type=Path, default=Path.cwd())
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
    gate = commands.add_parser("gate", help="Schedule and inspect approved issues")
    actions = gate.add_subparsers(dest="action", required=True)
    for name in ("tick", "status"):
        command = actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
        if name == "tick":
            command.add_argument("--no-start", action="store_true")
        else:
            command.add_argument("--live", action="store_true",
                                 help="Read GitHub once and refresh the local snapshot first")
    watch_command = actions.add_parser("watch", help="Watch the lane conveyor")
    watch_command.add_argument("--repo-root", type=Path, default=Path.cwd())
    watch_command.add_argument("--once", action="store_true")
    watch_command.add_argument("--open", action="store_true", help="Open the view in a split or new window and return")
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
    orchestrator = commands.add_parser("orch", help="Diagnose and recover blocked issues")
    orch_actions = orchestrator.add_subparsers(dest="action", required=True)
    for name in ("list", "rerun-review", "retry", "resolve", "escalate", "log", "watch", "premerge", "merge"):
        command = orch_actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        if name == "list":
            command.add_argument("--json", action="store_true")
        elif name == "watch":
            command.add_argument("--interval", type=float, default=5, help="Poll interval in seconds (default: 5)")
            command.add_argument("--follow", action="store_true", help="Keep printing changes until interrupted")
        elif name != "log":
            command.add_argument("number", type=int)
        if name == "premerge":
            command.add_argument("--json", action="store_true")
        elif name == "merge":
            command.add_argument("--approved-by", required=True)
            command.add_argument("--at")
        elif name == "retry":
            command.add_argument("--hint")
        elif name == "resolve":
            command.add_argument("--verdict", choices=("pass", "fail"), required=True)
            command.add_argument("--note", required=True)
        elif name == "escalate":
            command.add_argument("--summary", required=True)
            command.add_argument("--tried", required=True)
            command.add_argument("--option", action="append", default=[])
            command.add_argument("--recommend", type=int)
    request = orch_actions.add_parser("request", help="Put a planning request in the durable box")
    request.add_argument("--from", dest="source", required=True, help="Sending session, e.g. planning")
    request.add_argument("--text", required=True)
    inbox = orch_actions.add_parser("inbox", help="List requests in the box (read this first on start)")
    inbox.add_argument("--pending", action="store_true", help="Hide requests marked done")
    request_status = orch_actions.add_parser("request-status", help="Mark a request processing or done")
    request_status.add_argument("id")
    request_status.add_argument("status", choices=orch_inbox.REQUEST_STATUSES)
    request_status.add_argument("--note")
    ask = orch_actions.add_parser("ask", help="Record a D71 human question in the box")
    ask.add_argument("--question", required=True)
    ask.add_argument("--option", action="append", default=[])
    ask.add_argument("--recommend", type=int)
    questions = orch_actions.add_parser("questions", help="List open human questions")
    questions.add_argument("--all", action="store_true", help="Include answered questions")
    answer = orch_actions.add_parser("answer", help="Record the answer and close one question")
    answer.add_argument("id")
    answer.add_argument("--text", required=True)
    note = orch_actions.add_parser("note", help="Record what the orchestrator does now, why, next and where")
    note.add_argument("--now", required=True)
    note.add_argument("--why")
    note.add_argument("--next", dest="next_step")
    note.add_argument("--where-title", "--orca-title", dest="where_title", help="Orca tab title, e.g. orch: <project>")
    note.add_argument("--tmux-pane", help="tmux pane id when not taken from TMUX_PANE")
    for command in (request, inbox, request_status, ask, questions, answer, note):
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        if command in (inbox, questions):
            command.add_argument("--json", action="store_true")
    plan_command = commands.add_parser("plan", help="Planning-session signals for the orchestrator")
    plan_actions = plan_command.add_subparsers(dest="action", required=True)
    heartbeat = plan_actions.add_parser(
        "heartbeat", help="Mark the planning session alive; questions reach it by message, not push")
    heartbeat.add_argument("--repo-root", type=Path, default=Path.cwd())
    tidy_command = commands.add_parser("tidy", help="Check from local facts whether tidying is due")
    tidy_actions = tidy_command.add_subparsers(dest="action", required=True)
    tidy_due = tidy_actions.add_parser("due", help="Report whether tidying is due and why")
    tidy_due.add_argument("--json", action="store_true")
    tidy_due.add_argument("--repo-root", type=Path, default=Path.cwd())
    tidy_done = tidy_actions.add_parser("done", help="Record a finished tidy and start a new count")
    tidy_done.add_argument("--repo-root", type=Path, default=Path.cwd())
    briefing = commands.add_parser("brief", help="Show the start-of-session facts for planning")
    briefing.add_argument("--json", action="store_true")
    briefing.add_argument("--repo-root", type=Path, default=Path.cwd())
    # Listed for `fa --help` only; main() hands `fa test ...` to pytest before parsing.
    test = commands.add_parser(
        "test", help="Wait for a machine-wide test slot, then run pytest with the given arguments",
        usage=TEST_USAGE, add_help=False,
    )
    test.add_argument("pytest_args", nargs=argparse.REMAINDER)
    notification = commands.add_parser("notify", help="Send an alert through the configured channel")
    notification.add_argument("--title", required=True)
    notification.add_argument("--line", required=True)
    notification.add_argument("--link")
    notification.add_argument("--repo-root", type=Path, default=Path.cwd())
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


INBOX_ACTIONS = {"request", "inbox", "request-status", "ask", "questions", "answer", "note"}


def _tidy(args: argparse.Namespace, store: StateStore, language: str) -> None:
    """Local tidy schedule; never reads GitHub or starts an executor."""
    if args.action == "done":
        facts = tidy.done(store)
        print(text("tidy_done", language, at=facts["last_tidy_at"]))
        return
    facts = tidy.status(store)
    if args.json:
        print(json.dumps(tidy.public(facts), ensure_ascii=False))
        return
    last = facts["last_tidy_at"] or text("tidy_never", language)
    key = "tidy_due" if facts["due"] else "tidy_not_due"
    print(text(key, language, reason=facts["reason"], merges=facts["merges_since"], last=last))
    for line in facts["evidence"]:
        print(f"- {line}")


def _orch_inbox(args: argparse.Namespace, store: StateStore, config: Config, repo: str) -> None:
    """Local request/question box; never reads GitHub or starts an executor."""
    if args.action == "request":
        record = orch_inbox.add_request(store, args.source, args.text)
        print(f"Request {record['id']} saved ({record['status']}).")
    elif args.action == "request-status":
        record = orch_inbox.set_request_status(store, args.id, args.status, args.note)
        print(f"Request {record['id']}: {record['status']}.")
    elif args.action == "note":
        where = orch_inbox.note_location(dict(os.environ), args.where_title, args.tmux_pane)
        record = orch_inbox.write_note(store, args.now, args.why, args.next_step, where)
        print(f"Note saved at {record['at']} ({where['kind']}).")
    elif args.action == "ask":
        record = orch_inbox.ask(store, args.question, args.option, args.recommend)
        pushed = orch_inbox.route_question(store, config, repo, record)
        print(f"Question {record['id']} saved (open)"
              + ("; no planning session is alive, so a notification was sent." if pushed else "."))
    elif args.action == "answer":
        record = orch_inbox.answer(store, args.id, args.text)
        print(f"Question {record['id']} closed.")
    elif args.action == "inbox":
        rows = orch_inbox.requests(store, pending=args.pending)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No requests.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] from {row['from']} · {row['created_at']}")
            print(row["text"])
            for note in row.get("notes", []):
                print(f"  {note['at']} {note['status']}: {note['note']}")
    else:
        rows = orch_inbox.questions(store, include_closed=args.all)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No open questions.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] {row['question']}")
            for index, option in enumerate(row["options"], 1):
                mark = " (recommended)" if index == row["recommend"] else ""
                print(f"  {index}. {option}{mark}")
            if row.get("answer"):
                print(f"  answer: {row['answer']}")


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
    """Local status; the tracker is accepted for compatibility and never read."""
    return collect_status(store, config or Config(), datetime.now(UTC))


def _refresh_status(tracker: Tracker, store: StateStore, config: Config) -> None:
    """Read GitHub once for --live; a failure is recorded and the stale snapshot is shown."""
    context = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC))
    try:
        RecordingTracker(tracker, context).refresh_issues()
    except TrackerError:
        pass


def _print_status(status: dict[str, Any], language: str = "en") -> None:
    if status_unknown(status):
        print(text("status_unknown", language) + (f": {status['error']}" if status.get("error") else ""))
    print(text("summary", language, **summary_values(status["summary"])))
    if status.get("as_of"):
        print(text("status_as_of", language, time=status["as_of"]))
    print(f"launchd: {'loaded' if status['launchd']['loaded'] else 'not loaded'}")
    for lane in status["lanes"].values():
        details = [text(lane["state"], language)]
        if lane["issue"] is not None:
            details.append(f"#{lane['issue']} {lane['title']}")
        if lane["phase"]:
            details.append(text(lane["phase"], language))
        for field, key in (("elapsed_seconds", "elapsed"), ("last_activity_seconds", "activity")):
            if lane[field] is not None:
                details.append(text(key, language, seconds=int(lane[field])))
        if lane["waiting_for"]:
            details.append(text("waiting_for", language, issues=", ".join(f"#{n}" for n in lane["waiting_for"])))
        print(f"{lane['lane_display']}: {' · '.join(details)}")
    for state, issues in status["issues"].items():
        print(f"{state}:")
        if state in {RUNNING, READY}:
            for lane, grouped in status["lanes"].items():
                if grouped[state]:
                    print(f"  {grouped['lane_display']}:")
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
                summary = f"[{item.group}] {item.lane_display} · #{item.number} {item.title} — {item.summary}"
                if item.group == "try":
                    print(f"{summary} {item.url}")
                    for line in inspect_data.try_lines(item):
                        print("  " + line.replace("\n", "\n  "))
                    continue
                print(summary)
                for line in item.details.splitlines()[:12]:
                    print(f"  {line}")
                print(f"  {item.url}")


def _open_lane_view(root: Path) -> None:
    """Load the completed setup without making an optional view fail the command."""
    if os.environ.get("FORAGENT_VIEW") == "off":
        return
    language = "en"
    try:
        config = load_config(root)
        language = config.language
        repo = config.repo or GitRepo(root).remote_slug()
        watch_open.open_view(root, StateStore.for_repo(repo), language)
    except (ConfigError, GitError, OSError) as exc:
        error = " ".join(str(exc).split())[:200]
        print("foragent: " + text("watch_open_failed", language, error=error), file=sys.stderr)


TEST_USAGE = "fa test [--repo-root DIR] <pytest args>"


def _test_command(argv: list[str]) -> int:
    """`fa test`: everything after `test` goes to pytest, except a leading --repo-root."""
    root = None
    if argv[:1] == ["--repo-root"] and len(argv) > 1:
        root, argv = Path(argv[1]), argv[2:]
    elif argv and argv[0].startswith("--repo-root="):
        root, argv = Path(argv[0].split("=", 1)[1]), argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if root is None:
        cwd = Path.cwd()
        root = next((path for path in (cwd, *cwd.parents) if (path / ".foragent.yml").is_file()), cwd)
    root = root.resolve()
    try:
        return run_tests(root, load_config(root), argv)
    except (ConfigError, OSError, subprocess.SubprocessError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["test"]:
        return _test_command(argv[1:])
    args = _parser().parse_args(argv)
    try:
        if args.command == "design":
            if args.action == "lint":
                return design.run(args)
            root = args.repo_root.resolve()
            config = load_config(root)
            repo = config.repo or GitRepo(root).remote_slug()
            return design_session.run(args, StateStore.for_repo(repo))
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
        root = args.repo_root.resolve()
        if args.command == "profile":
            print(json.dumps(set_profile(root, kind=args.kind, stage=args.stage, priority=args.priority),
                             ensure_ascii=False))
            return 0
        if args.command == "order" and args.action == "progress":
            config = load_config(root)
            repo = config.repo or GitRepo(root).remote_slug()
            fields = {name: getattr(args, name) for name in
                      ("title", "add", "chunk", "slot", "ask", "filled", "done", "total", "issue")
                      if hasattr(args, name)}
            print(json.dumps(order.progress(StateStore.for_repo(repo), args.step, **fields),
                             ensure_ascii=False))
            return 0
        if args.command == "order" and args.action == "preread":
            config = load_config(root)
            results = order.preread(order.read_plan(args.plan), root, config,
                                    lambda: GhTracker(config.repo or GitRepo(root).remote_slug()))
            if args.json:
                print(json.dumps({"issues": results}, ensure_ascii=False))
            else:
                for result in results:
                    print(f"{result['key']}: " + ("no unknowns" if not result["unknowns"] else
                                                  f"{len(result['unknowns'])} unknown(s)"))
                    for unknown in result["unknowns"]:
                        print(f"  - {unknown}")
            return 0
        if args.command == "tidy":
            config = load_config(root)
            store = StateStore.for_repo(config.repo or GitRepo(root).remote_slug())
            _tidy(args, store, config.language)
            return 0
        if args.command in {"plan", "brief"}:
            config = load_config(root)
            store = StateStore.for_repo(config.repo or GitRepo(root).remote_slug())
            if args.command == "plan":
                record = orch_inbox.write_plan_heartbeat(store)
                print(f"Planning session alive at {record['at']}.")
            else:
                facts = brief.collect(store)
                print(json.dumps(facts, ensure_ascii=False) if args.json
                      else brief.render(facts, config.language))
            return 0
        if args.command == "orch" and args.action in INBOX_ACTIONS:
            config = load_config(root)
            repo = config.repo or GitRepo(root).remote_slug()
            _orch_inbox(args, StateStore.for_repo(repo), config, repo)
            return 0
        if args.command == "init":
            report = init_repo(
                root, approvers=args.approvers, review=args.review, executor=args.executor,
                run_hours=args.run_hours, no_push=args.no_push, no_launchd=args.no_launchd,
                dry_run=args.dry_run, language=args.language, no_board=args.no_board,
                board_map=args.board_map,
            )
            print(report.summary())
            if not args.dry_run:
                _open_lane_view(report.normalized_root or root)
            return 0
        if args.command == "lanes":
            if args.action == "skills":
                lane = set_lane_skills(root, args.name, args.ids)
                print(f"Updated lane:{args.name} skills: {', '.join(lane['skills']) or '(none)'}")
                return 0
            lane = add_lane(root, args.name, args.paths, skills=args.skills, executor=args.executor, model=args.model,
                            title=args.title, emoji=args.emoji)
            print(f"Created lane:{args.name} ({', '.join(lane['paths'])})")
            _open_lane_view(root)
            return 0
        if args.command == "notify":
            config = load_config(root)
            repo = config.repo or GitRepo(root).remote_slug()
            notify.send(replace(config, repo=repo), args.title, args.line, args.link)
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
                tracker = RunContext(repo=repo, repo_root=root, tracker=tracker,
                                     store=StateStore.for_repo(repo), config=config, git=git,
                                     now=lambda: datetime.now(UTC)).tracker
                plan = order.read_plan(args.plan)
                print(order.publish(
                    plan, root, config, tracker, repo=repo, git=git,
                    approve=args.approve, approve_keys=args.approve_keys,
                    approve_numbers=args.approve_numbers, dry_run=args.dry_run,
                ).summary())
                if not args.dry_run:
                    _open_lane_view(root)
            return 0
        store = StateStore.for_repo(repo)
        if args.command == "orch":
            ctx = RunContext(repo=repo, repo_root=root, tracker=tracker, store=store,
                             config=config, git=git, now=lambda: datetime.now(UTC))
            if args.action == "list":
                items = orch.list_blocked(ctx)
                if args.json:
                    print(json.dumps(items, ensure_ascii=False))
                elif not items:
                    print("No blocked issues.")
                else:
                    for item in items:
                        print(f"#{item['number']} {item['title']} [{item['lane']}] "
                              f"{item['reason'] or 'unknown'} · {item['blocked_at'] or 'unknown time'}")
                        print(item["details"])
                        for review in item["reviews"]:
                            print(f"Review: {review['path']}\n{review['text']}")
                        print(f"Log (last 60 lines): {item['log_path']}\n{item['log_tail']}")
            elif args.action == "log":
                for event in orch.log(ctx):
                    print(f"{event['at']} [{event['kind']}] {event['summary']}")
            elif args.action == "watch":
                _open_lane_view(root)
                unknown = False
                try:
                    for snapshot in orch.watch(ctx, interval=args.interval, follow=args.follow):
                        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
                        unknown = bool(snapshot.get("unknown"))
                except KeyboardInterrupt:
                    pass
                if unknown and not args.follow:
                    return 1
            elif args.action == "retry":
                print(orch.retry(ctx, args.number, args.hint))
            elif args.action == "rerun-review":
                print(orch.rerun_review(ctx, args.number))
            elif args.action == "resolve":
                print(orch.resolve(ctx, args.number, args.verdict, args.note))
            elif args.action == "escalate":
                print(orch.escalate(ctx, args.number, args.summary, args.tried, args.option, args.recommend))
            elif args.action == "premerge":
                report = orch.premerge(ctx, args.number)
                if args.json:
                    print(json.dumps(report, ensure_ascii=False))
                else:
                    print(f"PR #{report['pr']}: tests {report['tests']}; "
                          f"conflicts: {', '.join(report['conflicts']) or 'none'}; "
                          f"CI {report['ci']}; ready: {str(report['ready']).lower()}")
                return 0 if report["ready"] else 1
            elif args.action == "merge":
                print(orch.merge(ctx, args.number, approved_by=args.approved_by, at=args.at))
            return 0
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
                baseline_launcher=start_baseline_process,
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
            if args.live:
                _refresh_status(tracker, store, config)
            status = _status(tracker, store, config)
            status["launchd"] = asdict(launchd.status(root, repo=repo))
            status["test_slots"] = capacity.effective_test_slots(config.test_slots)
            if args.json:
                print(json.dumps(status))
            else:
                _print_status(status, config.language)
        elif args.action == "watch":
            if args.open:
                watch_open.open_view(root, store, config.language)
            else:
                observation = store.issues_observation()
                if observation["as_of"] is None and observation["error"] is None:
                    # Never observed: seed the snapshot once; refreshes stay local.
                    _refresh_status(tracker, store, config)
                watch.run(
                    lambda: _status(tracker, store, config), tracker, store, config, root,
                    once=args.once, now=lambda: datetime.now(UTC),
                )
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
    except (ConfigError, TrackerError, GitError, launchd.LaunchdError, OSError, design_session.DesignError,
            inspect_actions.InspectError, InspectRuntimeError, subprocess.SubprocessError,
            order.PlanError, orch.OrchError, orch_inbox.InboxError, LaneError, SkillError, PathError, json.JSONDecodeError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Acceptance test for talk-state. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os
from pathlib import Path
import shlex

from test_claude_sessions import SESSION_ID, session_case
from test_design_session import TerminalCommands
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


def test_talk_state_cli_records_talking_attaches_and_pauses_then_restarts_the_stall_clock(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from foragent.state import StateStore

    case = make_case(git_history, tmp_path, monkeypatch, stall_minutes=3,
                     skill_libraries=[], test_command="true")
    main = bind_cli(case, monkeypatch)
    terminals = TerminalCommands(monkeypatch, claude_reply=lambda argv, kwargs: json.dumps([
        {"id": SESSION_ID, "status": "running"},
    ]))
    gh = record_gh(monkeypatch)
    case.store.update_run(1, pid=88001, session_id=SESSION_ID)
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    log = case.store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("last executor output before the conversation\n", encoding="utf-8")
    timestamp = case.clock[0].timestamp()
    os.utime(log, (timestamp, timestamp))
    # Already quiet beyond the stall threshold when the person opens it.
    case.clock[0] += timedelta(minutes=5)
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
    assert StateStore(case.store.root).get_run(1).talking is True
    tabs = [argv for argv in terminals.calls if argv[1:3] == ["terminal", "create"]]
    assert len(tabs) == 1, terminals.calls
    command = shlex.split(tabs[0][tabs[0].index("--command") + 1])
    assert Path(command[0]).name == "claude"
    assert command[1:3] in [["--resume", SESSION_ID], ["attach", SESSION_ID]], command
    ctx = tick_context(case, start_runs=False)
    killed = []
    ctx.kill_runner = killed.append
    case.clock[0] += timedelta(minutes=30)
    report = tick(ctx)
    assert killed == [] and not any("stall" in action for action in report.actions)
    assert case.store.get_run(1).talking is True
    # A failed terminal-show fact, not a timeout on the conversation, ends talking.
    terminals.alive = False
    tick(ctx)
    run = case.store.get_run(1)
    assert run.talking is False and run.status == "running" and killed == []
    assert any(argv[1:3] == ["terminal", "show"] for argv in terminals.calls)
    case.clock[0] += timedelta(minutes=2)
    tick(ctx)
    assert killed == []
    case.clock[0] += timedelta(minutes=2)
    report = tick(ctx)
    assert killed == [88001] and any("stall" in action for action in report.actions)
    assert gh == []


def test_talk_state_interactive_issue_starts_in_a_visible_orca_tab(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case, sessions = session_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["interactive"])
    terminals = TerminalCommands(monkeypatch, claude_reply=sessions.reply)

    def visible_session():
        sessions.worktree = case.store.worktree_path(1)
        sessions.output = case.store.log_path(1).with_suffix(".stdout")
        sessions.output.parent.mkdir(parents=True, exist_ok=True)
        sessions.output.write_text(json.dumps({"session_id": SESSION_ID}) + "\n", encoding="utf-8")

    terminals.on_create = visible_session
    outcome = run_issue(1, case.ctx)
    tabs = [argv for argv in terminals.calls if argv[1:3] == ["terminal", "create"]]
    assert len(tabs) == 1, "interactive issues must open their session visibly from the start"
    command = shlex.split(tabs[0][tabs[0].index("--command") + 1])
    assert Path(command[0]).name == "claude" and "--bg" not in command and "-p" not in command
    assert not any("--bg" in argv for argv in sessions.calls)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert case.store.get_run(1).session_id == SESSION_ID


def test_talk_state_codex_rejection_is_one_line_and_leaves_state_and_terminals_untouched(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude",
                     skill_libraries=[], test_command="true")
    main = bind_cli(case, monkeypatch)
    terminals = TerminalCommands(monkeypatch)
    gh = record_gh(monkeypatch)
    before = {path.relative_to(case.store.root): path.read_bytes() for path in case.store.root.rglob("*") if path.is_file()}
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root)]) != 0
    output = capsys.readouterr()
    lines = (output.out + output.err).strip().splitlines()
    assert len(lines) == 1 and "codex" in lines[0].lower(), lines
    after = {path.relative_to(case.store.root): path.read_bytes() for path in case.store.root.rglob("*") if path.is_file()}
    assert after == before and terminals.calls == [] and gh == []

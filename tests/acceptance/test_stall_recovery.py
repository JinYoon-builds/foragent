"""Acceptance test for stall-recovery (D54). Locked by Foragent; do not edit."""

import json
import os
import subprocess
from datetime import timedelta


def _context(case, killed):
    from foragent.scheduler import TickContext

    return TickContext(repo="owner/repo", repo_root=case.root, tracker=case.tracker,
                       store=case.store, config=case.ctx.config, git=case.git,
                       now=lambda: case.clock[0], launcher=lambda n: 7000 + n,
                       runner_process_exists=lambda n: False, kill_runner=killed.append)


def _events(case):
    path = case.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_crashed_runner_group_is_killed_and_the_recovery_is_recorded(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    process = subprocess.Popen(["true"])
    process.wait()
    case.store.update_run(1, pid=process.pid)
    killed = []
    report = tick(_context(case, killed))
    assert killed == [process.pid], report.actions  # orphaned executors share the runner's group
    assert any("#1 runner crashed" in action for action in report.actions)
    assert any(e["issue"] == 1 and e["kind"] == "crash-restart" for e in _events(case)), _events(case)


def test_stall_restart_is_recorded(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, stall_minutes=3)
    old = (case.clock[0] - timedelta(minutes=5)).isoformat()
    case.store.update_run(1, pid=os.getpid(), started_at=old, last_progress_at=old)
    killed = []
    report = tick(_context(case, killed))
    assert killed == [os.getpid()]
    assert any("returned to ready after stall" in action for action in report.actions), report.actions
    event = next(e for e in _events(case) if e["issue"] == 1 and e["kind"] == "stall-restart")
    assert event["summary"] and event["at"]

"""Acceptance test for stall-visibility (D54). Locked by Foragent; do not edit."""

import os
import subprocess
from dataclasses import replace
from datetime import timedelta


def _api_row(output):
    rows = [line for line in output.splitlines() if line.startswith("│🧩 Api") and "#1" in line]
    assert rows, output
    return rows[0]


def test_dead_runner_shows_as_stalled_at_once(git_history, tmp_path, monkeypatch, capsys):
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch)
    process = subprocess.Popen(["true"])
    process.wait()
    now = case.clock[0].isoformat()
    case.store.update_run(1, pid=process.pid, last_activity_at=now, last_progress_at=now)
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    assert "○" in _api_row(capsys.readouterr().out)


def test_stalled_mark_follows_stall_minutes(git_history, tmp_path, monkeypatch, capsys):
    from foragent import cli
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch)
    config = replace(case.ctx.config, stall_minutes=3)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    recent = (case.clock[0] - timedelta(minutes=4)).isoformat()
    case.store.update_run(1, pid=os.getpid(), last_activity_at=recent, last_progress_at=recent)
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    assert "○" in _api_row(capsys.readouterr().out)

    fresh = (case.clock[0] - timedelta(minutes=1)).isoformat()
    case.store.update_run(1, last_activity_at=fresh, last_progress_at=fresh)
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    assert "○" not in _api_row(capsys.readouterr().out)


def test_notification_line_names_the_issue_and_the_reason(git_history, tmp_path, monkeypatch):
    from foragent.config import Config
    from foragent.inspect_actions import notify
    from foragent.inspect_data import InspectContext
    from foragent.reasons import Reason, format_comment
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked", "needs-human"])
    case.tracker.comment(1, format_comment(Reason.ORCHESTRATOR_TIMEOUT,
                                           "The orchestrator has not responded to this blocker.",
                                           "Latest blocker: Runner stalled again after one restart.",
                                           label="needs-human"))
    sent = []
    ctx = InspectContext(case.tracker, case.store, "owner/repo", Config(repo="owner/repo"), lambda: case.clock[0])
    line = notify(ctx, notifier=lambda line, title: sent.append(line))
    assert sent and line == sent[0]
    assert "#1" in line and "Implement feature" in line, line

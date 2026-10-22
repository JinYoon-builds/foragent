"""Acceptance test for view-evidence. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os

from test_heartbeats import heartbeats_offline, heartbeats_write


def view_evidence_case(git_history, tmp_path, monkeypatch):
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language="ko")
    for number in (2, 3, 4):
        case.tracker.close_issue(number)
    case.store.update_run(1, pid=88001, phase="implementing",
                          last_activity_at=case.clock[0].isoformat(),
                          last_progress_at=case.clock[0].isoformat())
    monkeypatch.setattr("foragent.watch.os.kill", lambda pid, signal: None)
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(seconds=10))
    heartbeats_write(case.store, "gate", case.clock[0])
    view_evidence_log(case, minutes=1)
    return case, main


def view_evidence_log(case, *, minutes):
    path = case.store.log_path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("executor progress\n")
    modified = (case.clock[0] - timedelta(minutes=minutes)).timestamp()
    os.utime(path, (modified, modified))
    return path


def view_evidence_snapshot(case, main, capsys):
    from foragent.watch import collect

    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    return collect(status, case.tracker, case.store, case.config, case.clock[0])


def view_evidence_worker_row(frame):
    rows = [line for line in frame.splitlines() if line.startswith("│🧩 Api") and "#1" in line]
    assert len(rows) == 1, frame
    return rows[0]


def test_view_evidence_orchestrator_claim_follows_heartbeat_age(git_history, tmp_path, monkeypatch, capsys):
    from foragent.watch import render_frame

    case, main = view_evidence_case(git_history, tmp_path, monkeypatch)
    fresh = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "지켜보는 중" in fresh
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=2))
    stale = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "오케스트레이터가 꺼져 있어요" in stale and "⚠" in stale
    assert "지켜보는 중" not in stale


def test_view_evidence_working_and_quiet_use_log_age_rather_than_fresh_state_metadata(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case, main = view_evidence_case(git_history, tmp_path, monkeypatch)
    # Old state metadata must not override actual, recent executor output.
    case.store.update_run(1, last_activity_at=(case.clock[0] - timedelta(minutes=12)).isoformat())
    fresh = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    row = view_evidence_worker_row(fresh)
    assert "●" in row and "조용해요" not in row and "멈춘 듯" not in row, row
    # Conversely, fresh state metadata is not evidence of executor output.
    case.store.update_run(1, last_activity_at=case.clock[0].isoformat())
    view_evidence_log(case, minutes=10)
    quiet = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "10분째 조용해요" in view_evidence_worker_row(quiet)


def test_view_evidence_dead_pid_overrides_fresh_log_and_working_record(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case, main = view_evidence_case(git_history, tmp_path, monkeypatch)

    def dead(pid, signal):
        assert pid == 88001 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr("foragent.watch.os.kill", dead)
    frame = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "작업자가 없어요" in view_evidence_worker_row(frame) and "⚠" in frame


def test_view_evidence_stale_gate_warns_on_first_line(git_history, tmp_path, monkeypatch, capsys):
    from foragent.watch import render_frame

    case, main = view_evidence_case(git_history, tmp_path, monkeypatch)
    fresh = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "⚠" not in fresh.splitlines()[0]
    heartbeats_write(case.store, "gate", case.clock[0] - timedelta(minutes=20))
    stale = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "⚠" in stale.splitlines()[0]


def test_view_evidence_idle_without_heartbeats_has_no_orchestrator_warning(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case, main = view_evidence_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    for name in ("orch", "gate"):
        (case.store.root / f"{name}-heartbeat.json").unlink()
    frame = render_frame(view_evidence_snapshot(case, main, capsys), case.config, case.root,
                         width=90, now=case.clock[0])
    assert "⚠" not in frame and "오케스트레이터가 꺼져 있어요" not in frame
    assert "지켜보는 중" not in frame, "absence of work must not invent a live watcher"

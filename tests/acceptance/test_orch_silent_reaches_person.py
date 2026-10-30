"""Acceptance test for orch-silent-person. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from foragent.config import load_config
from foragent.orch_spawn import ensure_orchestrator
from foragent.state import StateStore
from test_heartbeats import heartbeats_offline, heartbeats_write  # noqa: F401
from test_orch_review_handoff import bind_cli
from test_orch_seat import existing_spawn_suppression, read_seat, write_seat
from test_orch_spawn_liveness import launcher_returns_pid, liveness_case  # noqa: F401
from test_stop_whole_tree import events
from test_stop_whole_tree import fake_processes  # noqa: F401 -- offline autouse fixture


def assert_silence_push(push, case, language):
    line = push["line"]
    phrase = ("오케스트레이터가 조용해요" if language == "ko"
              else "The orchestrator is running but silent")
    assert phrase in line, "Use the configured language's stable silence phrase"
    assert "claude agents" in line and f"orch: {case.root.name}" in line, (
        "The person must be able to find the invisible session"
    )
    if language == "ko":
        assert "실행 중" in line and "질문" in line and "기다" in line
    else:
        assert "question" in line and "waiting" in line and "see" in line


@pytest.mark.parametrize("language", ["ko", "en"])
def test_live_silence_pushes_once_persists_and_rearms_only_after_fresh_heartbeat(
    liveness_case, fake_processes, language,
):
    case, commands, pushes = liveness_case
    case.config = replace(case.config, language=language)
    session = fake_processes.add(
        43000, cwd=case.root.resolve(), start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "--bg"),
    )
    write_seat(case, session, taken_at=case.clock[0] - timedelta(hours=1))
    heartbeat = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=29))
    before = heartbeat.read_bytes()

    def tick(store=None):
        assert ensure_orchestrator(case.root, store or case.store, case.config, now=case.clock[0]) is None
        assert commands == [], "A live silent session must never launch a replacement"
        assert not fake_processes.signals

    tick()
    assert pushes == [], "Twenty-nine minutes of silence is below the default threshold"
    first_events = [event for event in events(case) if event["kind"] == "orch-silent"]
    assert len(first_events) == 1, "Keep the existing hourly orch-silent diagnostic"
    case.clock[0] += timedelta(minutes=2)
    tick()
    assert len(pushes) == 1, "A live orchestrator silent for 31 minutes must push once to the person"
    assert_silence_push(pushes[0], case, language)
    case.clock[0] += timedelta(minutes=10)
    tick()
    assert len(pushes) == 1
    case.clock[0] += timedelta(minutes=60)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    tick(reopened)
    assert len(pushes) == 1, "The already-pushed fact must survive StateStore reopening and hourly notes"
    silent_events = [event for event in events(case) if event["kind"] == "orch-silent"]
    assert len(silent_events) == 2, "Hourly diagnostics must continue during the same silence episode"
    assert heartbeat.read_bytes() == before, "Silence detection must not renew the orchestrator's heartbeat"

    # The gate sees an actually fresh heartbeat before a second silence begins.
    heartbeats_write(case.store, "orch", case.clock[0])
    tick(reopened)
    case.clock[0] += timedelta(minutes=29)
    tick(reopened)
    assert len(pushes) == 1
    case.clock[0] += timedelta(minutes=2)
    reopened_again = StateStore(case.store.root, now=lambda: case.clock[0])
    tick(reopened_again)
    assert len(pushes) == 2, "A fresh heartbeat followed by 31 minutes of silence must allow one new push"
    assert_silence_push(pushes[1], case, language)


def test_no_heartbeat_measures_silence_from_recorded_spawn_time(
    liveness_case, fake_processes, monkeypatch,
):
    case, commands, pushes = liveness_case
    pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    # Historical locked name retained. A real gate launch now establishes the
    # seat-taking time; orch-spawn.json no longer determines silence identity/origin.
    holder = read_seat(case)["holder"]
    assert (holder["pid"], holder["start_time"]) == (pid, start_time)
    assert holder["taken_at"] == case.clock[0].isoformat()
    (case.store.root / "orch-heartbeat.json").unlink(missing_ok=True)
    fake_processes.add(
        pid, cwd=case.root.parent / "session-host", start_time=start_time,
        argv=("/offline/bin/claude", "--bg"),
    )
    commands.clear()
    pushes.clear()
    case.clock[0] += timedelta(minutes=29)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert pushes == []
    case.clock[0] += timedelta(minutes=2)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1, "Without a heartbeat, 31 minutes since taking the seat must trigger a push"
    assert_silence_push(pushes[0], case, "ko")
    assert commands == [] and not fake_processes.signals


def test_team_setting_is_listed_configurable_and_zero_disables_only_the_push(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    main = bind_cli(case, monkeypatch)
    key = "orch_silent_alert_minutes"
    assert main(["settings", "list", "--json", "--repo-root", str(case.root)]) == 0
    listed = json.loads(capsys.readouterr().out)
    entry = next((item for item in listed if item["key"] == key), None)
    assert entry is not None, "fa settings list must register orch_silent_alert_minutes"
    assert entry["scope"] == "team" and entry["default"] == entry["value"] == 30
    assert getattr(load_config(case.root), key) == 30
    session = fake_processes.add(
        43000, cwd=case.root.resolve(), start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "--bg"),
    )
    write_seat(case, session, taken_at=case.clock[0] - timedelta(hours=1))
    assert main(["settings", "set", key, "7", "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    config = load_config(case.root)
    assert getattr(config, key) == 7
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=6))
    assert ensure_orchestrator(case.root, case.store, config, now=case.clock[0]) is None
    assert pushes == []
    case.clock[0] += timedelta(minutes=2)
    assert ensure_orchestrator(case.root, case.store, config, now=case.clock[0]) is None
    assert len(pushes) == 1, "The configured threshold must replace the default"

    assert main(["settings", "set", key, "0", "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    disabled = load_config(case.root)
    assert getattr(disabled, key) == 0
    heartbeats_write(case.store, "orch", case.clock[0])
    assert ensure_orchestrator(case.root, case.store, disabled, now=case.clock[0]) is None
    pushes.clear()
    case.clock[0] += timedelta(hours=2)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, disabled, now=case.clock[0]) is None
    assert pushes == [], "Zero must disable silence pushes even in a new episode after reopening"
    assert commands == [] and not fake_processes.signals
    assert any(event["kind"] == "orch-silent" for event in events(case)), "Zero must retain the hourly event"


@pytest.mark.parametrize("evidence", ["fresh", "recorded-session", "argv-match"])
def test_no_live_orchestrator_keeps_normal_spawn_and_sends_no_silence_push(
    liveness_case, fake_processes, monkeypatch, evidence,
):
    case, commands, pushes = liveness_case
    pid, _ = launcher_returns_pid(case, fake_processes, monkeypatch)
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=2))
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    assert len(commands) == 1 and "/fa-orchestrator" in commands[0]
    assert len(pushes) == 1 and "오케스트레이터를 새로 열었어요" in pushes[0]["line"]
    assert "오케스트레이터가 조용해요" not in pushes[0]["line"]
    assert not any(event["kind"] == "orch-silent" for event in events(case))

    # Without a seat holder, legacy evidence may suppress another spawn but
    # cannot identify a #952 silence episode. Preserve these existing guards.
    fake_processes.exit(pid)
    (case.store.root / "orch-seat.json").unlink(missing_ok=True)
    case.clock[0] += timedelta(hours=3)
    existing_spawn_suppression(case, fake_processes, evidence)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(commands) == len(pushes) == 1, (
        "Legacy evidence still blocks spawning, but no seat holder means no silence push"
    )
    assert not (case.store.root / "orch-seat.json").exists(), "A spawn inhibitor is not a seat assignment"
    assert not fake_processes.signals


@pytest.mark.parametrize("origin", ["heartbeat", "seat", "no-heartbeat"])
def test_silence_uses_latest_heartbeat_or_seat_time_and_ignores_legacy_identity_facts(
    liveness_case, fake_processes, origin,
):
    case, commands, pushes = liveness_case
    session = fake_processes.add(
        43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 36000,
        argv=("/offline/bin/claude", "--bg"),
    )
    seat_age = 180 if origin == "heartbeat" else 29
    write_seat(case, session, taken_at=case.clock[0] - timedelta(minutes=seat_age))
    if origin == "no-heartbeat":
        (case.store.root / "orch-heartbeat.json").unlink()
    else:
        heartbeat_age = 29 if origin == "heartbeat" else 180
        path = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=heartbeat_age))
        # These remain valid spawn inhibitors. Neither the recorded session
        # nor the argv scan identifies the seat holder or the silence episode.
        record = json.loads(path.read_text(encoding="utf-8"))
        record["session"] = {"pid": 44000, "start_time": case.clock[0].timestamp()}
        record["session_id"] = "unrelated-heartbeat-session"
        path.write_text(json.dumps(record), encoding="utf-8")
    fake_processes.add(44000, cwd=case.root, start_time=case.clock[0].timestamp(),
                       argv=("/offline/bin/claude", "/fa-orchestrator"))
    (case.store.root / "orch-spawn.json").write_text(json.dumps({
        "pid": 44000, "start_time": case.clock[0].timestamp(),
        "spawned_at": case.clock[0].isoformat(), "session_id": "unrelated-spawn-session",
    }), encoding="utf-8")

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert pushes == [], "The later of heartbeat at and seat taken_at is only 29 minutes old"
    case.clock[0] += timedelta(minutes=1)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1, "The 30-minute threshold is inclusive and ignores newer legacy identity facts"
    assert_silence_push(pushes[0], case, "ko")
    assert commands == [] and not fake_processes.signals


@pytest.mark.parametrize("change", ["new-pid", "reused-pid"])
def test_seat_holder_change_starts_a_new_episode_without_a_fresh_heartbeat(
    liveness_case, fake_processes, change,
):
    case, commands, pushes = liveness_case
    old = fake_processes.add(43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 36000,
                             argv=("/offline/bin/claude", "--bg"))
    write_seat(case, old, taken_at=case.clock[0] - timedelta(minutes=31))
    heartbeat = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=3))
    before = heartbeat.read_bytes()
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1
    pid = 45000 if change == "new-pid" else old.pid
    new = fake_processes.add(pid, cwd=case.root.parent, start_time=old.start_time + 60,
                             argv=("/offline/bin/claude", "--bg"))
    write_seat(case, new)  # A handover now, even if the new process was born hours ago.
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1, "A newly seated holder must get its own silence interval"
    case.clock[0] += timedelta(minutes=29)
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1
    case.clock[0] += timedelta(minutes=2)
    reopened_again = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened_again, case.config, now=case.clock[0]) is None
    assert len(pushes) == 2, "A different PID/start_time must rearm without any fresh heartbeat"
    assert heartbeat.read_bytes() == before
    assert commands == [] and not fake_processes.signals


def test_legacy_session_identity_changes_do_not_rearm_an_unchanged_seat_holder(
    liveness_case, fake_processes,
):
    case, commands, pushes = liveness_case
    session = fake_processes.add(43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 7200,
                                 argv=("/offline/bin/claude", "--bg"))
    write_seat(case, session, taken_at=case.clock[0] - timedelta(minutes=31))
    heartbeat = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=3))
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1
    legacy = fake_processes.add(44000, cwd=case.root, start_time=case.clock[0].timestamp(),
                                argv=("/offline/bin/claude", "/fa-orchestrator"))
    record = json.loads(heartbeat.read_text(encoding="utf-8"))
    record.update(session={"pid": legacy.pid, "start_time": legacy.start_time}, session_id="another-session")
    heartbeat.write_text(json.dumps(record), encoding="utf-8")
    seat_path = case.store.root / "orch-seat.json"
    seat = json.loads(seat_path.read_text(encoding="utf-8"))
    seat["holder"]["session_id"] = "now-known-session-id"
    seat_path.write_text(json.dumps(seat), encoding="utf-8")
    (case.store.root / "orch-spawn.json").write_text(json.dumps({
        "pid": legacy.pid, "start_time": legacy.start_time,
        "spawned_at": case.clock[0].isoformat(), "session_id": "another-spawn",
    }), encoding="utf-8")
    case.clock[0] += timedelta(minutes=31)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1, "Only a holder PID/start_time change or observed fresh heartbeat rearms"
    assert commands == [] and not fake_processes.signals


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_failed_notify_is_retried_until_sent_once_for_the_holder(
    liveness_case, fake_processes, monkeypatch, failure,
):
    from foragent import notify

    case, commands, pushes = liveness_case
    session = fake_processes.add(43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 7200,
                                 argv=("/offline/bin/claude", "--bg"))
    write_seat(case, session, taken_at=case.clock[0] - timedelta(minutes=31))
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=3))
    attempts = []

    def send(config, title, line, link=None):
        attempts.append({"title": title, "line": line})
        if len(attempts) == 1:
            if failure == "exception":
                raise OSError("offline notification failure")
            return False
        return True

    monkeypatch.setattr(notify, "send", send)
    for _ in range(3):
        reopened = StateStore(case.store.root, now=lambda: case.clock[0])
        assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
        case.clock[0] += timedelta(seconds=1)
    assert len(attempts) == 2, "False/raising sends are not persisted as delivered; a successful send is"
    assert_silence_push(attempts[1], case, "ko")
    assert commands == pushes == [] and not fake_processes.signals

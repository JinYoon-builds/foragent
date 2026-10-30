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
from test_orch_spawn_liveness import liveness_case, record_heartbeat_session  # noqa: F401
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
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=29))
    heartbeat = record_heartbeat_session(case, pid=session.pid, start_time=session.start_time)
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
    record_heartbeat_session(case, pid=session.pid, start_time=session.start_time)
    tick(reopened)
    case.clock[0] += timedelta(minutes=29)
    tick(reopened)
    assert len(pushes) == 1
    case.clock[0] += timedelta(minutes=2)
    reopened_again = StateStore(case.store.root, now=lambda: case.clock[0])
    tick(reopened_again)
    assert len(pushes) == 2, "A fresh heartbeat followed by 31 minutes of silence must allow one new push"
    assert_silence_push(pushes[1], case, language)


def test_no_heartbeat_measures_silence_from_recorded_spawn_time(liveness_case, fake_processes):
    case, commands, pushes = liveness_case
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    # Use the real public launch path's spawn record, not an invented record shape.
    (case.store.root / "orch-heartbeat.json").unlink(missing_ok=True)
    fake_processes.add(
        43000, cwd=case.root.resolve(), start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )
    commands.clear()
    pushes.clear()
    case.clock[0] += timedelta(minutes=29)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert pushes == []
    case.clock[0] += timedelta(minutes=2)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(pushes) == 1, "Without a heartbeat, 31 minutes since the recorded spawn must trigger a push"
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
    fake_processes.add(
        43000, cwd=case.root.resolve(), start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )
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


def test_no_live_orchestrator_keeps_normal_spawn_and_sends_no_silence_push(liveness_case):
    case, commands, pushes = liveness_case
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=2))
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    assert len(commands) == 1 and "/fa-orchestrator" in commands[0]
    assert len(pushes) == 1 and "오케스트레이터를 새로 열었어요" in pushes[0]["line"]
    assert "오케스트레이터가 조용해요" not in pushes[0]["line"]
    assert not any(event["kind"] == "orch-silent" for event in events(case))

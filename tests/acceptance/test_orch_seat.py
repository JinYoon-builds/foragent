"""Acceptance test for orch-seat. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from foragent import orch
from foragent.orch_spawn import ensure_orchestrator
from foragent.state import StateStore
from test_heartbeats import heartbeats_offline, heartbeats_write  # noqa: F401
from test_orch_refresh import OLD_SESSION, accept_note, fa_ok, transcript
from test_orch_review_handoff import bind_cli
from test_orch_spawn_liveness import (  # noqa: F401
    launcher_returns_pid, liveness_case, record_heartbeat_session,
)
from test_skills_autosync import cli_exit
from test_stop_whole_tree import fake_processes  # noqa: F401 -- offline autouse fixture


# Small wire contract shared with #952, not a prescribed Python API:
# <state>/orch-seat.json = {"holder": {"pid": int, "start_time": float,
#   "taken_at": ISO8601, "session_id": str when known}, "previous": old_holder?}.
# `fa orch seat --json` reports these holder/previous objects plus `alive`.
# A missing lease reports holder=null and alive=false, without taking the seat.

REFUSAL = {
    "ko": "다른 오케스트레이터가 이미 자리를 잡고 있어요",
    "en": "Another orchestrator already holds the seat",
}


def write_seat(case, process, *, taken_at=None, session_id=None):
    """Seed the documented durable input; all decisions use the real entry points."""
    holder = {"pid": process.pid, "start_time": process.start_time,
              "taken_at": (taken_at or case.clock[0]).isoformat()}
    if session_id is not None:
        holder["session_id"] = session_id
    path = case.store.root / "orch-seat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holder": holder}), encoding="utf-8")
    return holder


def read_seat(case):
    path = case.store.root / "orch-seat.json"
    assert path.is_file(), "An orchestrator launch or accepted follow watch must persist orch-seat.json"
    return json.loads(path.read_text(encoding="utf-8"))


def reopen(case):
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store


def seat_report(case, monkeypatch, capsys):
    main = bind_cli(case, monkeypatch)
    capsys.readouterr()
    code = cli_exit(main, ["orch", "seat", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch seat --json must report the lease (exit={code}): {output.out}{output.err}"
    return json.loads(output.out)


def as_watcher(case, table, monkeypatch, *, session_id=OLD_SESSION, process=None):
    """Same fake ancestor table as the existing watch/liveness acceptance tests."""
    session = process or table.add(
        table.grandparent_pid, cwd=case.root, start_time=case.clock[0].timestamp() - 600,
        argv=("/offline/bin/claude",),
    )
    table.add(table.parent_pid, ppid=session.pid, cwd=case.root,
              start_time=case.clock[0].timestamp() - 20, argv=("/bin/sh",))
    table.add(table.self_pid, ppid=table.parent_pid, cwd=case.root,
              start_time=case.clock[0].timestamp() - 10, argv=("fa", "orch", "watch", "--follow"))
    monkeypatch.setenv("FORAGENT_SESSION_ID", session_id)
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    return session


def follow_once(case, monkeypatch, capsys):
    main = bind_cli(case, monkeypatch)
    sleeps = []

    def stop(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setattr(orch.time, "sleep", stop)
    capsys.readouterr()
    code = cli_exit(main, ["orch", "watch", "--follow", "--interval", "1",
                           "--repo-root", str(case.root)])
    output = capsys.readouterr()
    return code, output.out + output.err, sleeps


def assert_refused(result, language):
    code, output, sleeps = result
    assert code != 0, "A different live seat holder must refuse watch --follow"
    assert len(output.strip().splitlines()) == 1, "Seat refusal must print exactly one localized line"
    assert REFUSAL[language] in output
    assert sleeps == [], "A refused watch must exit before entering its polling loop"


def existing_spawn_suppression(case, table, evidence):
    """Seed one of today's spawn inhibitors, without assigning a seat holder."""
    heartbeat = case.store.root / "orch-heartbeat.json"
    heartbeat.unlink(missing_ok=True)
    process = None
    if evidence == "fresh":
        heartbeats_write(case.store, "orch", case.clock[0])
    elif evidence in {"recorded-session", "argv-match"}:
        process = table.add(
            44000, cwd=case.root if evidence == "argv-match" else case.root.parent / "session-host",
            start_time=case.clock[0].timestamp() - 3600,
            argv=("/offline/bin/claude", "/fa-orchestrator") if evidence == "argv-match"
            else ("/offline/bin/claude",),
        )
        heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=3))
        if evidence == "recorded-session":
            record_heartbeat_session(case, pid=process.pid, start_time=process.start_time)

    def clear():
        heartbeat.unlink(missing_ok=True)
        if process is not None:
            table.exit(process.pid)

    return clear


@pytest.mark.parametrize("heartbeat", ["missing", "fresh", "argv-match", "recorded-session"])
def test_no_seat_gate_spawns_and_persists_identity_across_reopening(
    liveness_case, fake_processes, monkeypatch, capsys, heartbeat,
):
    case, commands, pushes = liveness_case
    clear_evidence = existing_spawn_suppression(case, fake_processes, heartbeat)
    if heartbeat != "missing":
        before = (case.store.root / "orch-heartbeat.json").read_bytes()
        for _ in range(2):
            assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None, (
                "Existing live evidence must still suppress a spawn when no lease exists"
            )
            assert commands == pushes == []
            assert not (case.store.root / "orch-seat.json").exists(), (
                "Legacy spawn suppression must not invent a seat holder"
            )
            assert (case.store.root / "orch-heartbeat.json").read_bytes() == before
            reopen(case)
    clear_evidence()
    pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background", (
        "An absent seat permits a spawn only after all existing live evidence is gone"
    )
    assert len(commands) == 1
    holder = read_seat(case)["holder"]
    assert (holder["pid"], holder["start_time"]) == (pid, start_time)
    assert holder["taken_at"] == case.clock[0].isoformat()
    argv = commands[0]
    assert holder["session_id"] == argv[argv.index("--session-id") + 1]
    assert len(pushes) == 1 and "오케스트레이터를 새로 열었어요" in pushes[0]["line"]

    # Remove the launcher's incidental argv/cwd evidence, retaining its lifetime.
    # Suppression after reopening must now come from the lease itself.
    fake_processes.add(pid, cwd=case.root.parent / "session-host", start_time=start_time,
                       argv=("/offline/bin/claude", "--bg"))
    case.clock[0] += timedelta(hours=3)  # Far past both heartbeat freshness and spawn spacing.
    reopen(case)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert len(commands) == 1, "A live lease must suppress another launch after reopening and cooldown"
    report = seat_report(case, monkeypatch, capsys)
    assert report["holder"] == holder and report["alive"] is True
    assert not fake_processes.signals


def test_live_holder_suppresses_spawn_with_three_hour_stale_wrong_heartbeat(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    holder_process = fake_processes.add(
        43000, cwd=case.root.parent / "session-host", start_time=case.clock[0].timestamp() - 18000,
        argv=("/offline/bin/claude", "--bg"),
    )
    holder = write_seat(case, holder_process, taken_at=case.clock[0] - timedelta(hours=4))
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=3))
    # r47: the heartbeat names another lifetime; it cannot override the lease.
    record_heartbeat_session(case, pid=44000, start_time=1)
    before = (case.store.root / "orch-heartbeat.json").read_bytes()
    for _ in range(2):
        assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None, (
            "A live lease holder must prevent a second session despite a stale, unrelated heartbeat"
        )
        assert commands == []
        assert not any("오케스트레이터를 새로 열었어요" in push["line"] for push in pushes)
        reopen(case)
        case.clock[0] += timedelta(hours=3)
    assert read_seat(case)["holder"] == holder
    report = seat_report(case, monkeypatch, capsys)
    assert report["alive"] is True and report["holder"] == holder
    assert (case.store.root / "orch-heartbeat.json").read_bytes() == before
    assert not fake_processes.signals


@pytest.mark.parametrize("lifetime", ["dead", "reused"])
@pytest.mark.parametrize("evidence", ["missing", "fresh", "recorded-session", "argv-match"])
def test_dead_or_reused_holder_spawns_despite_fresh_heartbeat(
    liveness_case, fake_processes, monkeypatch, lifetime, evidence,
):
    # Historical test name retained. A fresh heartbeat now blocks the spawn;
    # replacement is allowed only after it and every other live fact are gone.
    case, commands, pushes = liveness_case
    old = fake_processes.add(43000, cwd=case.root, start_time=case.clock[0].timestamp() - 14400,
                             argv=("/offline/bin/claude",))
    write_seat(case, old, taken_at=case.clock[0] - timedelta(hours=3))
    if lifetime == "dead":
        fake_processes.exit(old.pid)
    else:
        fake_processes.add(old.pid, cwd=case.root, start_time=old.start_time + 60,
                           argv=("/offline/bin/claude",))
    clear_evidence = existing_spawn_suppression(case, fake_processes, evidence)
    before_seat = (case.store.root / "orch-seat.json").read_bytes()
    if evidence != "missing":
        for _ in range(2):
            assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None, (
                "A dead/reused lease cannot override an existing live-orchestrator suppression"
            )
            assert commands == pushes == []
            assert (case.store.root / "orch-seat.json").read_bytes() == before_seat
            reopen(case)
    clear_evidence()
    pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)
    reopen(case)

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background", (
        "A dead/reused holder permits a replacement only when no existing live evidence remains"
    )
    assert len(commands) == len(pushes) == 1
    holder = read_seat(case)["holder"]
    assert (holder["pid"], holder["start_time"]) == (pid, start_time), (
        "A replacement launch must persist the new holder's PID and start_time"
    )
    assert holder["taken_at"] == case.clock[0].isoformat()
    assert not fake_processes.signals


@pytest.mark.parametrize("language", ["ko", "en"])
def test_follow_refuses_a_different_live_holder_in_one_localized_line(
    liveness_case, fake_processes, monkeypatch, capsys, language,
):
    case, commands, pushes = liveness_case
    case.config = case.ctx.config = replace(case.config, language=language)
    process = fake_processes.add(43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 3600,
                                 argv=("/offline/bin/claude", "--bg"))
    write_seat(case, process, taken_at=case.clock[0] - timedelta(minutes=30))
    as_watcher(case, fake_processes, monkeypatch)
    before_seat = (case.store.root / "orch-seat.json").read_bytes()
    before_heartbeat = (case.store.root / "orch-heartbeat.json").read_bytes()
    reopen(case)

    assert_refused(follow_once(case, monkeypatch, capsys), language)
    assert (case.store.root / "orch-seat.json").read_bytes() == before_seat
    assert (case.store.root / "orch-heartbeat.json").read_bytes() == before_heartbeat
    assert commands == pushes == [] and not fake_processes.signals


@pytest.mark.parametrize("previous", ["absent", "dead", "reused"])
def test_follow_takes_a_free_seat_and_same_holder_does_not_reset_taken_at(
    liveness_case, fake_processes, monkeypatch, capsys, previous,
):
    case, commands, pushes = liveness_case
    if previous != "absent":
        old = fake_processes.add(43000, cwd=case.root, start_time=case.clock[0].timestamp() - 7200)
        write_seat(case, old, taken_at=case.clock[0] - timedelta(hours=1))
        if previous == "dead":
            fake_processes.exit(old.pid)
        else:
            fake_processes.add(old.pid, cwd=case.root, start_time=old.start_time + 60)
    current = as_watcher(case, fake_processes, monkeypatch)
    code, output, sleeps = follow_once(case, monkeypatch, capsys)
    assert code == 0 and sleeps == [1], output
    holder = read_seat(case)["holder"]
    assert (holder["pid"], holder["start_time"]) == (current.pid, current.start_time)
    assert holder["session_id"] == OLD_SESSION and holder["taken_at"] == case.clock[0].isoformat()

    case.clock[0] += timedelta(minutes=10)
    reopen(case)
    code, output, sleeps = follow_once(case, monkeypatch, capsys)
    assert code == 0 and sleeps == [1], "The same lifetime may resume its watch: " + output
    assert read_seat(case)["holder"] == holder, "Re-entering watch must retain the seat-taking time"
    report = seat_report(case, monkeypatch, capsys)
    assert report["holder"] == holder and report["alive"] is True
    assert commands == pushes == [] and not fake_processes.signals


def test_seat_json_reports_absent_live_dead_and_reused_without_taking_the_seat(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    report = seat_report(case, monkeypatch, capsys)
    assert report["holder"] is None and report["alive"] is False
    assert not (case.store.root / "orch-seat.json").exists()
    process = fake_processes.add(43000, cwd=case.root.parent, start_time=case.clock[0].timestamp() - 3600)
    holder = write_seat(case, process)
    before = (case.store.root / "orch-seat.json").read_bytes()
    for state, alive in (("live", True), ("dead", False), ("reused", False)):
        if state == "dead":
            fake_processes.exit(process.pid)
        elif state == "reused":
            fake_processes.add(process.pid, cwd=case.root, start_time=process.start_time + 60)
        reopen(case)
        report = seat_report(case, monkeypatch, capsys)
        assert report["holder"] == holder and report["alive"] is alive, state
        assert (case.store.root / "orch-seat.json").read_bytes() == before
    assert commands == pushes == [] and not fake_processes.signals


def test_refresh_ready_hands_seat_to_new_session_and_old_follow_refuses(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    old = as_watcher(case, fake_processes, monkeypatch)
    previous = write_seat(case, old, taken_at=case.clock[0] - timedelta(minutes=5), session_id=OLD_SESSION)
    path = case.root.parent / "old-transcript.jsonl"
    transcript(path, OLD_SESSION, case.clock[0] - timedelta(hours=1), 4)
    heartbeat = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(hours=1))
    heartbeat.write_text(json.dumps({"at": (case.clock[0] - timedelta(hours=1)).isoformat(),
                                    "session_id": OLD_SESSION, "transcript_path": str(path),
                                    "started_at": (case.clock[0] - timedelta(hours=1)).isoformat(),
                                    "status": "active"}), encoding="utf-8")
    case.main = bind_cli(case, monkeypatch)
    accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)
    pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)
    fa_ok(case, capsys, "refresh", "--ready")

    seat = read_seat(case)
    holder = seat["holder"]
    assert (holder["pid"], holder["start_time"]) == (pid, start_time), (
        "refresh --ready must replace the lease holder even while the outgoing holder is alive"
    )
    assert seat["previous"] == previous, "The outgoing holder must be durably recorded as previous"
    assert holder["taken_at"] == case.clock[0].isoformat()
    assert len(commands) == 1
    argv = commands[0]
    assert holder["session_id"] == argv[argv.index("--session-id") + 1] != OLD_SESSION
    assert json.loads(heartbeat.read_text(encoding="utf-8"))["session_id"] == holder["session_id"]
    assert old.pid in fake_processes.live and pid in fake_processes.live
    reopen(case)
    report = seat_report(case, monkeypatch, capsys)
    assert report["holder"] == holder and report["previous"] == previous and report["alive"] is True

    before_heartbeat = heartbeat.read_bytes()
    assert_refused(follow_once(case, monkeypatch, capsys), "ko")
    assert heartbeat.read_bytes() == before_heartbeat
    assert read_seat(case) == seat
    case.clock[0] += timedelta(hours=3)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert len(commands) == 1 and not fake_processes.signals

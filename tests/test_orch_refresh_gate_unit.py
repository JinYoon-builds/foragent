"""Unit boundaries of the gate's seat-holder replacement (r1210): due() and seat retirement."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from foragent import orch_refresh_gate, orch_seat, processes
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc)
OLD = {"pid": 43000, "start_time": 1000.0}
NEW = {"pid": 88001, "start_time": 2000.0}


@pytest.fixture
def live(monkeypatch):
    state = {"alive": True}
    monkeypatch.setattr(processes, "recorded_identity_evidence",
                        lambda pid, start, fresh=True: SimpleNamespace(alive=state["alive"]))
    return state


@pytest.fixture
def store(tmp_path, live):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.root.mkdir(parents=True)
    return store


def seat(store, holder, *, taken_at, session_id="old-session"):
    lease = {"holder": {**holder, "taken_at": taken_at.isoformat(), "session_id": session_id}}
    orch_seat.path(store).write_text(json.dumps(lease), encoding="utf-8")
    return lease["holder"]


def heartbeat(store, *, at, session_id="old-session", started_at=None):
    (store.root / "orch-heartbeat.json").write_text(json.dumps({
        "at": at.isoformat(), "session_id": session_id, "transcript_path": "/offline/t.jsonl",
        "started_at": (started_at or at).isoformat(), "status": "active",
    }), encoding="utf-8")


def test_due_when_the_holders_own_heartbeat_is_silent_past_the_threshold(store):
    seat(store, OLD, taken_at=NOW - timedelta(hours=3))
    heartbeat(store, at=NOW - timedelta(minutes=29), started_at=NOW - timedelta(hours=3))
    assert orch_refresh_gate.due(store, Config(), NOW) is None
    later = NOW + timedelta(minutes=1)
    assert orch_refresh_gate.due(store, Config(), later)["why"].startswith("silent")
    assert orch_refresh_gate.due(store, Config(orch_silent_alert_minutes=0), later) is None


def test_silence_starts_at_the_later_of_heartbeat_and_taken_at(store):
    seat(store, OLD, taken_at=NOW - timedelta(minutes=5))
    heartbeat(store, at=NOW - timedelta(hours=2), started_at=NOW - timedelta(hours=2))
    assert orch_refresh_gate.due(store, Config(), NOW) is None


def test_heartbeat_naming_another_session_or_a_dead_holder_is_not_due(store, live):
    seat(store, OLD, taken_at=NOW - timedelta(hours=3))
    heartbeat(store, at=NOW - timedelta(hours=3), session_id="other-session")
    assert orch_refresh_gate.due(store, Config(), NOW) is None
    heartbeat(store, at=NOW - timedelta(hours=3))
    assert orch_refresh_gate.due(store, Config(), NOW) is not None
    live["alive"] = False
    assert orch_refresh_gate.due(store, Config(), NOW) is None


@pytest.mark.parametrize("minutes", [31, 180])
@pytest.mark.parametrize("previous_beat", [True, False])
def test_a_holder_that_never_beat_is_silent_from_taken_at(store, minutes, previous_beat):
    """Startup silence counts: no own first heartbeat, timed from the seat's taken_at."""
    taken = NOW - timedelta(minutes=minutes)
    seat(store, OLD, taken_at=taken)
    if previous_beat:  # Only the replaced session's older heartbeat remains (activate keeps its `at`).
        heartbeat(store, at=taken - timedelta(hours=1), started_at=taken)
    else:  # No heartbeat `at` at all, only the activated descriptor.
        heartbeat(store, at=taken, started_at=taken)
        record = json.loads((store.root / "orch-heartbeat.json").read_text(encoding="utf-8"))
        del record["at"]
        (store.root / "orch-heartbeat.json").write_text(json.dumps(record), encoding="utf-8")
    due = orch_refresh_gate.due(store, Config(), NOW)
    assert due is not None and due["why"].startswith("silent")
    assert orch_refresh_gate.due(store, Config(), taken + timedelta(minutes=29)) is None


def test_due_when_the_holders_session_is_older_than_refresh_hours_although_it_beats(store):
    seat(store, OLD, taken_at=NOW - timedelta(hours=30))
    heartbeat(store, at=NOW - timedelta(seconds=5), started_at=NOW - timedelta(hours=24))
    assert orch_refresh_gate.due(store, Config(), NOW)["why"].startswith("older")
    assert orch_refresh_gate.due(store, Config(orch_refresh_hours=25), NOW) is None


def test_a_retired_lifetime_never_takes_the_free_seat_back(store, monkeypatch):
    holder = seat(store, OLD, taken_at=NOW - timedelta(hours=3))
    assert orch_seat.retire(store, holder, NOW) is True
    assert orch_seat.read(store) == (None, None) and not orch_seat.blocks_spawn(store)
    with pytest.raises(orch_seat.SeatHeld):
        orch_seat.beat(store, holder, lambda: pytest.fail("a retired holder must not beat"))
    monkeypatch.setattr(orch_seat, "_me", lambda: dict(OLD))
    with pytest.raises(orch_seat.SeatHeld):
        orch_seat.take_for_watch(store, NOW)
    monkeypatch.setattr(orch_seat, "_me", lambda: dict(NEW))
    taken = orch_seat.take_for_watch(store, NOW)
    lease = json.loads(orch_seat.path(store).read_text(encoding="utf-8"))
    assert (taken["pid"], taken["start_time"]) == (NEW["pid"], NEW["start_time"])
    assert lease["previous"]["pid"] == OLD["pid"] and lease["retired"][0]["pid"] == OLD["pid"]


def test_retire_refuses_a_moved_lease_and_hand_over_keeps_the_retired_list(store):
    holder = seat(store, OLD, taken_at=NOW - timedelta(hours=3))
    other = {**holder, "pid": 50000}
    before = orch_seat.path(store).read_bytes()
    assert orch_seat.retire(store, other, NOW) is False
    assert orch_seat.path(store).read_bytes() == before
    assert orch_seat.retire(store, holder, NOW) is True
    assert orch_seat.hand_over(store, NEW["pid"], NEW["start_time"], "new-session", NOW) is True
    lease = json.loads(orch_seat.path(store).read_text(encoding="utf-8"))
    assert lease["holder"]["session_id"] == "new-session"
    assert lease["previous"]["pid"] == OLD["pid"] and [row["pid"] for row in lease["retired"]] == [OLD["pid"]]
    with pytest.raises(orch_seat.SeatHeld):
        orch_seat.beat(store, holder, lambda: pytest.fail("the old holder lost the seat"))

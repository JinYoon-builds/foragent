"""Boundary cases of the orchestrator seat lease (#962) the locked acceptance file leaves open."""

from datetime import UTC, datetime
import json

import pytest

from foragent import orch_seat, processes
from foragent.state import StateStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
HOLDER = {"pid": 43000, "start_time": 100.0, "taken_at": "2026-10-30T11:00:00+00:00"}


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: NOW)


def liveness(monkeypatch, alive):
    monkeypatch.setattr(processes, "recorded_identity_evidence",
                        lambda pid, start, **kw: processes.IdentityEvidence(alive))


def seed(store, lease):
    store.root.mkdir(parents=True, exist_ok=True)
    orch_seat.path(store).write_text(lease if isinstance(lease, str) else json.dumps(lease))


def test_corrupt_lease_is_absent_with_a_reason_and_frees_the_seat(store, monkeypatch):
    liveness(monkeypatch, True)
    seed(store, "{not json")
    assert orch_seat.report(store) == {"holder": None, "alive": False, "reason": "corrupt"}
    assert orch_seat.blocks_spawn(store) is False


def test_unknown_liveness_reports_null_blocks_spawn_and_refuses_watch(store, monkeypatch):
    liveness(monkeypatch, None)
    seed(store, {"holder": HOLDER})
    assert orch_seat.report(store) == {"holder": HOLDER, "alive": None, "reason": "unknown"}
    assert orch_seat.blocks_spawn(store) is True
    monkeypatch.setattr(orch_seat, "_me", lambda: {"pid": 1, "start_time": 2.0})
    with pytest.raises(orch_seat.SeatHeld):
        orch_seat.take_for_watch(store, NOW)
    assert json.loads(orch_seat.path(store).read_text()) == {"holder": HOLDER}


def test_dead_holder_becomes_previous_and_unreadable_launch_writes_no_lease(store, monkeypatch):
    liveness(monkeypatch, False)
    seed(store, {"holder": HOLDER})
    assert orch_seat.blocks_spawn(store) is False
    assert orch_seat.hand_over(store, None, 5.0, "s", NOW) is False
    assert json.loads(orch_seat.path(store).read_text()) == {"holder": HOLDER}
    assert orch_seat.hand_over(store, 88001, 5.0, "s", NOW) is True
    assert json.loads(orch_seat.path(store).read_text()) == {
        "holder": {"pid": 88001, "start_time": 5.0, "taken_at": NOW.isoformat(), "session_id": "s"},
        "previous": HOLDER}


def test_session_replaces_its_own_launcher_and_keeps_the_handover_previous(store, monkeypatch):
    liveness(monkeypatch, True)
    launcher = {"pid": 88001, "start_time": 5.0, "taken_at": NOW.isoformat()}
    seed(store, {"holder": launcher, "previous": HOLDER})
    monkeypatch.setattr(orch_seat, "_me", lambda: {"pid": 90000, "start_time": 6.0})
    monkeypatch.setattr(orch_seat, "_ancestor", lambda holder: holder == launcher)
    monkeypatch.setenv("FORAGENT_SESSION_ID", "new")
    holder = orch_seat.take_for_watch(store, NOW)
    assert holder == {"pid": 90000, "start_time": 6.0, "taken_at": NOW.isoformat(), "session_id": "new"}
    assert json.loads(orch_seat.path(store).read_text()) == {"holder": holder, "previous": HOLDER}


def test_unreadable_self_refuses_even_a_free_seat_so_two_watches_cannot_run(store, monkeypatch):
    monkeypatch.setattr(orch_seat, "_me", lambda: None)
    for _ in range(2):  # Neither of two unidentifiable watches may start.
        with pytest.raises(orch_seat.SeatHeld):
            orch_seat.take_for_watch(store, NOW)
    assert not orch_seat.path(store).exists()


def test_own_identity_falls_back_to_a_direct_read_of_this_process(monkeypatch):
    monkeypatch.setattr(processes, "claude_session", lambda: None)
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: None)

    class Own:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 7.5

    monkeypatch.setattr(processes.psutil, "Process", Own)
    assert orch_seat._me()["start_time"] == 7.5

    def unreadable(pid):
        raise processes.psutil.AccessDenied(pid)

    monkeypatch.setattr(processes.psutil, "Process", unreadable)
    assert orch_seat._me() is None


def test_holder_writes_a_vanished_or_corrupt_lease_back_before_its_heartbeat(store):
    mine = {"pid": 1, "start_time": 2.0, "taken_at": NOW.isoformat()}
    writes = []
    for broken in (None, "{not json"):
        if broken is None:
            orch_seat.path(store).unlink(missing_ok=True)
        else:
            seed(store, broken)
        orch_seat.beat(store, mine, lambda: writes.append(json.loads(orch_seat.path(store).read_text())))
    assert writes == [{"holder": mine}, {"holder": mine}]
    seed(store, {"holder": HOLDER})
    with pytest.raises(orch_seat.SeatHeld):
        orch_seat.beat(store, mine, lambda: writes.append("late"))
    assert len(writes) == 2


def test_same_holder_resumes_with_its_taken_at_and_gains_a_known_session_id(store, monkeypatch):
    liveness(monkeypatch, True)
    seed(store, {"holder": HOLDER, "previous": {"pid": 1, "start_time": 1.0, "taken_at": "x"}})
    monkeypatch.setattr(orch_seat, "_me", lambda: {"pid": HOLDER["pid"], "start_time": HOLDER["start_time"]})
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)
    assert orch_seat.take_for_watch(store, NOW) == HOLDER
    monkeypatch.setenv("FORAGENT_SESSION_ID", "sid")
    holder = orch_seat.take_for_watch(store, NOW)
    assert holder == {**HOLDER, "session_id": "sid"}
    assert json.loads(orch_seat.path(store).read_text()) == {
        "holder": holder, "previous": {"pid": 1, "start_time": 1.0, "taken_at": "x"}}


def test_seat_check_and_heartbeat_write_share_the_openers_lock(store):
    import fcntl
    import os

    mine = {"pid": 1, "start_time": 2.0, "taken_at": NOW.isoformat()}
    seed(store, {"holder": mine})
    busy = []

    def write():  # A refresh handover (same lock) cannot run between the check and this write.
        fd = os.open(store.root / "watch-open.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy.append(False)
        except BlockingIOError:
            busy.append(True)
        finally:
            os.close(fd)

    orch_seat.beat(store, mine, write)
    assert busy == [True]


def test_running_watch_stops_once_another_lifetime_holds_the_seat(store):
    mine = {"pid": 1, "start_time": 2.0, "taken_at": NOW.isoformat()}
    seed(store, {"holder": mine})
    assert orch_seat.still_holds(store, mine)
    seed(store, {"holder": HOLDER})
    assert not orch_seat.still_holds(store, mine)


def test_ensure_reports_seat_when_only_the_lease_stops_it(store, monkeypatch, tmp_path):
    from foragent import orch_ensure, orch_spawn

    liveness(monkeypatch, True)
    seed(store, {"holder": HOLDER})
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.setattr(orch_spawn, "_approved_work", lambda store: True)
    monkeypatch.setattr(orch_spawn, "_session_alive", lambda store, root: False)
    assert orch_ensure.skip_reason(tmp_path, store, NOW) == "seat"
    why = []
    assert orch_ensure.ensure_explicit(tmp_path, store, None, NOW, why) is None
    assert why == ["seat"] and (store.root / "orch-silent-note.json").is_file()


def silent_events(store):
    path = store.root / "orch-events.jsonl"
    lines = path.read_text().splitlines() if path.is_file() else []
    return [event for event in map(json.loads, lines) if event["kind"] == "orch-silent"]


def test_cli_ensure_stopped_only_by_the_seat_writes_the_silent_event(store, monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from foragent import orch_spawn
    from foragent.commands import _orch_box
    from foragent.config import Config

    liveness(monkeypatch, True)
    seed(store, {"holder": HOLDER})
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.setattr(orch_spawn, "_approved_work", lambda store: True)
    monkeypatch.setattr(orch_spawn, "_session_alive", lambda store, root: False)
    _orch_box._ensure(SimpleNamespace(repo_root=tmp_path), store, Config(repo="owner/repo"))
    assert "Opened nothing" in capsys.readouterr().out
    assert len(silent_events(store)) == 1


def test_gate_inside_lock_seat_stop_writes_the_silent_event(store, monkeypatch, tmp_path):
    from contextlib import contextmanager

    from foragent import orch_spawn, watch_open
    from foragent.config import Config

    liveness(monkeypatch, True)
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.setattr(orch_spawn, "_approved_work", lambda store: True)
    monkeypatch.setattr(orch_spawn, "_session_alive", lambda store, root: False)
    real = watch_open._opening

    @contextmanager
    def opening(target):  # The seat is taken while the gate waits for the lock.
        seed(store, {"holder": HOLDER})
        with real(target):
            yield

    monkeypatch.setattr(watch_open, "_opening", opening)
    assert orch_spawn.ensure_orchestrator(tmp_path, store, Config(repo="owner/repo"), now=NOW) is None
    assert len(silent_events(store)) == 1


def test_ensure_inside_lock_seat_stop_writes_the_silent_event(store, monkeypatch, tmp_path):
    from contextlib import contextmanager

    from foragent import orch_ensure, orch_spawn

    liveness(monkeypatch, True)
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.setattr(orch_spawn, "_approved_work", lambda store: True)
    monkeypatch.setattr(orch_spawn, "_session_alive", lambda store, root: False)
    real = orch_ensure._explicit_lock

    @contextmanager
    def explicit_lock(target):  # The seat is taken while ensure waits for the lock.
        seed(store, {"holder": HOLDER})
        with real(target) as busy:
            yield busy

    monkeypatch.setattr(orch_ensure, "_explicit_lock", explicit_lock)
    why = []
    assert orch_ensure.ensure_explicit(tmp_path, store, None, NOW, why) is None
    assert why == ["seat"] and len(silent_events(store)) == 1

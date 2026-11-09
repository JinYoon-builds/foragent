"""Durable ownership excludes duplicate effects without a long kernel hold."""

import json
import os
import threading

import pytest

from foragent import state_claims
from foragent.file_locks import file_lock


def test_work_holds_no_file_lock_and_another_thread_cannot_claim(tmp_path):
    path = tmp_path / "work.intent"
    with state_claims.hold(path) as owned:
        assert owned
        with file_lock(path.with_suffix(".lock"), timeout=0):
            pass
        results = []
        contender = threading.Thread(target=lambda: results.append(state_claims.acquire(path)))
        contender.start()
        contender.join(5)
        assert results == [None]
    with state_claims.hold(path) as owned:
        assert owned


def test_stopped_or_reused_process_is_reclaimed_but_live_or_unknown_is_not(tmp_path, monkeypatch):
    path = tmp_path / "work.intent"
    previous = {"pid": 12345, "start_time": 1, "token": "previous"}
    for observed, expected in ((1, False), (None, True), (2, True)):
        path.write_text(json.dumps(previous))
        monkeypatch.setattr(state_claims, "_identity", lambda pid: 10 if pid == os.getpid() else observed)
        with state_claims.hold(path) as owned:
            assert owned is expected
    path.write_text(json.dumps(previous))

    def unreadable(pid):
        if pid == os.getpid():
            return 10
        raise state_claims.psutil.AccessDenied(pid)

    monkeypatch.setattr(state_claims, "_identity", unreadable)
    assert state_claims.acquire(path) is None


def test_replacing_a_snapshot_during_the_process_probe_is_rechecked(tmp_path, monkeypatch):
    path = tmp_path / "work.intent"
    previous = {"pid": 12345, "start_time": 1, "token": "previous"}
    replacement = {"pid": 12346, "start_time": 2, "token": "replacement"}
    path.write_text(json.dumps(previous))

    def identity(pid):
        if pid == previous["pid"]:
            path.write_text(json.dumps(replacement))
            return None
        return 2

    monkeypatch.setattr(state_claims, "_identity", identity)
    assert state_claims.acquire(path) is None
    state_claims.release(path, "previous")
    assert json.loads(path.read_text()) == replacement


def test_child_adoption_survives_late_parent_handoff_and_releases_only_its_token(tmp_path, monkeypatch):
    path = tmp_path / "work.intent"
    token = state_claims.acquire(path)
    parent = os.getpid()
    monkeypatch.setattr(state_claims.os, "getpid", lambda: parent + 1)
    monkeypatch.setattr(state_claims, "_identity", lambda pid: pid)
    with state_claims.adopted(path, token) as owned:
        assert owned
        monkeypatch.setattr(state_claims.os, "getpid", lambda: parent)
        state_claims.handoff(path, token, parent + 1)
        assert json.loads(path.read_text())["pid"] == parent + 1
        # A parent's failed handoff must not release an already adopted child.
        state_claims.release(path, token, pid=parent)
        assert json.loads(path.read_text())["pid"] == parent + 1
        with state_claims.adopted(path, "replaced") as adopted:
            assert not adopted
        assert state_claims.acquire(path) is None
    assert not path.exists()


@pytest.mark.parametrize("transport", [int, str])
def test_legacy_descriptor_is_released_before_the_worker_runs(tmp_path, transport):
    path = tmp_path / "work.intent"
    descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    state_claims.fcntl.flock(descriptor, state_claims.fcntl.LOCK_EX)
    with state_claims.process_owner(path, transport(descriptor)) as owned:
        assert owned
        with file_lock(path.with_suffix(".lock"), timeout=0):
            pass
        assert state_claims.acquire(path) is None
    assert not path.exists()


def test_a_numeric_launch_token_is_not_treated_as_a_descriptor(tmp_path):
    path = tmp_path / "work.intent"
    state_claims.acquire(path)
    row = json.loads(path.read_text())
    row["token"] = "12345678901234567890123456789012"
    path.write_text(json.dumps(row))
    with state_claims.process_owner(path, row["token"]) as owned:
        assert owned
    assert not path.exists()


def test_handoff_of_a_child_that_stops_before_adoption_can_be_reclaimed(tmp_path, monkeypatch):
    path = tmp_path / "work.intent"
    parent, child = os.getpid(), os.getpid() + 1
    stopped = False
    monkeypatch.setattr(state_claims, "_identity", lambda pid: None if pid == child and stopped else pid)
    token = state_claims.acquire(path)
    with file_lock(path.with_suffix(".lock")):
        state_claims.handoff(path, token, child)
    assert state_claims.acquire(path) is None
    stopped = True
    successor = state_claims.acquire(path)
    assert successor is not None and successor != token
    assert state_claims._read(path)["pid"] == parent
    state_claims.release(path, token)
    assert state_claims._read(path)["token"] == successor
    state_claims.release(path, successor)
    assert not list(tmp_path.glob("*.child"))


def test_launch_failure_cleanup_is_nonblocking_and_reclaimable_while_parent_lives(tmp_path, monkeypatch):
    from foragent import file_locks

    path = tmp_path / "work.intent"
    token = state_claims.acquire(path)
    monkeypatch.setattr(file_locks.time, "sleep", lambda seconds: pytest.fail("nested cleanup waited"))
    with file_lock(path.with_suffix(".lock")):
        state_claims.release(path, token, pid=os.getpid())
    successor = state_claims.acquire(path)
    assert successor is not None and successor != token
    state_claims.release(path, successor)

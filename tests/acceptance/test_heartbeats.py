"""Acceptance test for heartbeats. Locked by Foragent; do not edit."""

import json
from datetime import timedelta

import pytest


@pytest.fixture(autouse=True)
def heartbeats_offline(tmp_path, monkeypatch):
    """Shared isolation for the D66/D67 tests, without changing conftest."""
    import socket
    import tempfile

    from foragent.state import StateStore

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(tmp_path / "notifications", **kwargs)))

    def no_network(*args, **kwargs):
        raise AssertionError("acceptance tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


def heartbeats_write(store, name, at):
    store.root.mkdir(parents=True, exist_ok=True)
    path = store.root / f"{name}-heartbeat.json"
    path.write_text(json.dumps({"at": at.isoformat()}), encoding="utf-8")
    return path


def test_heartbeats_watch_overwrites_on_first_and_unchanged_state_reads(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import orch
    from foragent.cli import main
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    tick(tick_context(case, start_runs=False))
    bind_cli(case, monkeypatch)
    path = case.store.root / "orch-heartbeat.json"
    assert not path.exists()
    reads = []

    def after_read(seconds):
        assert seconds == 3
        assert path.is_file(), "the first state read must create the orchestrator heartbeat"
        assert json.loads(path.read_text()) == {"at": case.clock[0].isoformat()}, (
            "every state read, including the first and unchanged polls, must renew the heartbeat")
        assert path.stat().st_size < 128, "heartbeats must overwrite, not append"
        reads.append(case.clock[0])
        if len(reads) == 40:
            raise KeyboardInterrupt
        case.clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(orch.time, "sleep", after_read)
    assert main(["orch", "watch", "--interval", "3", "--follow",
                 "--repo-root", str(case.root)]) == 0
    assert len(reads) == 40
    assert len(capsys.readouterr().out.splitlines()) == 1, "unchanged polls should stay silent"


def test_heartbeats_gate_overwrites_at_each_tick(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    ctx = tick_context(case, start_runs=False)
    path = case.store.root / "gate-heartbeat.json"
    for _ in range(20):
        tick(ctx)
        assert path.is_file(), "a gate tick must write its heartbeat under the StateStore root"
        assert json.loads(path.read_text()) == {"at": case.clock[0].isoformat()}
        assert path.stat().st_size < 128
        case.clock[0] += timedelta(seconds=1)


def test_heartbeats_store_returns_datetimes_and_missing_sides(tmp_path):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    assert store.heartbeats() == {"orchestrator": None, "gate": None}
    heartbeats_write(store, "orch", BASE_TIME)
    assert store.heartbeats() == {"orchestrator": BASE_TIME, "gate": None}
    heartbeats_write(store, "gate", BASE_TIME - timedelta(seconds=5))
    assert store.heartbeats() == {"orchestrator": BASE_TIME, "gate": BASE_TIME - timedelta(seconds=5)}


def test_heartbeats_alive_json_handles_absence_and_the_thirty_second_boundary(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.cli import main
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    bind_cli(case, monkeypatch)
    command = ["orch", "alive", "--json", "--repo-root", str(case.root)]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out) == {"alive": False, "last_seen": None, "age_seconds": None}
    for age, alive in ((0, True), (30, True), (31, False)):
        at = case.clock[0] - timedelta(seconds=age)
        heartbeats_write(case.store, "orch", at)
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out) == {
            "alive": alive, "last_seen": at.isoformat(), "age_seconds": age,
        }

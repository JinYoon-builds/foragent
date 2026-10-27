"""Boundary tests for the architect session record, liveness and executor options."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foragent import orch_architect, processes
from foragent.config import Config
from foragent.executors import codex_command
from foragent.processes import ProcessIdentity
from foragent.state import StateStore


def identity(pid, start):
    return ProcessIdentity(pid, 1, pid, pid, start)


def test_session_alive_matches_start_time_when_recorded(monkeypatch):
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: identity(pid, 10.0))
    assert orch_architect.session_alive({"pid": 4242, "start_time": 10.0})
    assert not orch_architect.session_alive({"pid": 4242, "start_time": 11.0}), "A reused pid is not the architect"


def test_session_alive_falls_back_to_signal_zero_probe(monkeypatch):
    def unreadable(pid):
        raise PermissionError(pid)

    probes = []

    def probe(pid, sig):
        probes.append((pid, sig))
        raise ProcessLookupError(pid)

    monkeypatch.setattr(processes, "read_process_identity", unreadable)
    monkeypatch.setattr(orch_architect.os, "kill", probe)
    assert not orch_architect.session_alive({"pid": 4242, "start_time": 10.0})
    assert not orch_architect.session_alive({"pid": 4242, "start_time": None})
    assert probes == [(4242, 0), (4242, 0)]
    assert not orch_architect.session_alive(None)
    assert not orch_architect.session_alive({"pid": "4242"})


def test_call_refuses_while_the_executor_is_paused(tmp_path):
    store = StateStore(tmp_path / "state")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    store.pause_executor("claude", now + timedelta(hours=1))
    with pytest.raises(orch_architect.ArchitectRefused, match="paused"):
        orch_architect.call(tmp_path, store, Config(executor="claude"), "evidence", now)
    assert not orch_architect.record_path(store).exists()


def test_session_options_use_tier_l_and_codex_only_gets_the_state_root(tmp_path):
    store = StateStore(tmp_path / "state")
    codex = orch_architect.session_options(Config(executor="codex"), store)
    assert codex["effort"] == Config().tiers["l"]["codex_effort"] and codex["add_dirs"] == (store.root,)
    claude = orch_architect.session_options(Config(executor="claude"), store)
    assert "add_dirs" not in claude and "effort" not in claude


def test_codex_command_adds_extra_writable_roots():
    argv = codex_command(Path("/repo"), Path("/out"), add_dirs=(Path("/state"),))
    assert argv[argv.index("--add-dir") + 1] == "/state"
    assert "--add-dir" not in codex_command(Path("/repo"), Path("/out"))


def test_run_architect_is_hidden_from_gate_help_usage_and_errors(capsys):
    from foragent import cli

    with pytest.raises(SystemExit):
        cli._parser().parse_args(["gate", "--help"])
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["gate", "no-such-verb"])
    output = capsys.readouterr()
    assert "run-issue" in output.out and "check-done" in output.err
    assert "run-architect" not in output.out + output.err and "SUPPRESS" not in output.out
    args = cli._parser().parse_args(["gate", "run-architect", "--repo-root", ".",
                                     "--started-at", "2026-10-27T00:00:00+00:00", "--why", "w"])
    assert args.action == "run-architect" and callable(args.handler)

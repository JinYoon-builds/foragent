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


@pytest.mark.parametrize("executor", ["claude", "codex"])
def test_run_session_keeps_the_final_message_in_log_and_event(tmp_path, monkeypatch, executor):
    import json

    from foragent import executors
    from foragent.executors import ExecResult

    store = StateStore(tmp_path / "state")
    started_at = "2026-10-27T01:02:03+00:00"
    calls = []

    class Fake:
        def run(self, **kwargs):
            calls.append(kwargs)
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            return ExecResult(0, False, "Nothing to change.\nfacts showed no hot file", kwargs["log_path"])

    monkeypatch.setattr(executors, "executor_for", lambda name: Fake())
    now = lambda: datetime(2026, 10, 27, 2, tzinfo=UTC)
    assert orch_architect.run_session(tmp_path, store, Config(executor=executor), "why", started_at, now) == 0
    log = orch_architect.log_path(store, started_at)
    assert "Nothing to change." in log.read_text(encoding="utf-8")
    event = json.loads((store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert event["kind"] == "architect-done" and event["summary"] == "exit code 0: Nothing to change."
    assert event["message"].startswith("Nothing to change.")
    expected = orch_architect.events_path(store, started_at) if executor == "codex" else None
    assert calls[0].get("events_path") == expected


def test_codex_executor_keeps_events_at_a_named_path(tmp_path, monkeypatch):
    from foragent import executors

    events = tmp_path / "architect.stdout"

    def fake_run_logged(argv, *, stdout_path, **kwargs):
        stdout_path.write_text('{"type":"thread.started","thread_id":"t1"}\n')
        return 0, False, None

    monkeypatch.setattr(executors, "run_logged", fake_run_logged)
    executors.CodexExecutor().run(worktree=tmp_path, prompt="p", model=None, timeout_s=1,
                                  log_path=tmp_path / "log", events_path=events)
    assert "thread.started" in events.read_text()

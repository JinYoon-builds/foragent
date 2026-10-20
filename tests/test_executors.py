from pathlib import Path
import json
import os
import signal
import subprocess
import sys

import pytest

from foragent.executors import (
    ClaudeExecutor, CodexExecutor, claude_command, codex_command, executor_for,
)
from foragent.processes import kill_process_group, run_logged


@pytest.mark.parametrize("model", [None, "model with spaces"])
def test_exact_codex_command(model):
    assert codex_command(Path("/work tree"), Path("/last message"), model) == [
        "codex", "exec", "-", "-C", "/work tree", "-s", "workspace-write",
        "--skip-git-repo-check", "--json", "-o", "/last message",
        *(["-m", model] if model is not None else []),
    ]


@pytest.mark.parametrize("model", [None, "other model"])
def test_exact_claude_command(model):
    assert claude_command(model) == [
        "claude", "-p", "--permission-mode", "auto", "--output-format", "text",
        *(["--model", model] if model is not None else []),
    ]


def test_executor_factory():
    assert isinstance(executor_for("codex"), CodexExecutor)
    assert isinstance(executor_for("claude"), ClaudeExecutor)
    with pytest.raises(ValueError, match="unknown executor"):
        executor_for("unknown")


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_executor_uses_worktree_stdin_append_log_and_output(kind, tmp_path, monkeypatch):
    gate_env = tmp_path / "gate/.venv"
    project_bin = tmp_path / "project/.venv/bin"
    monkeypatch.setattr(sys, "prefix", str(gate_env))
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.setenv(key, str(gate_env))
    monkeypatch.setenv("PATH", os.pathsep.join([str(gate_env / "bin"), str(project_bin), "/usr/bin"]))
    monkeypatch.setenv("FORAGENT_TEST_KEEP", "kept")
    calls = []
    log = tmp_path / "issue.log"
    log.write_text("earlier\n")

    class Process:
        pid = 123456
        returncode = 7

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            kwargs["stdout"].write(b"session stdout\n")
            if kwargs["stderr"] != subprocess.STDOUT:
                kwargs["stderr"].write(b"session stderr\n")
            if kind == "codex":
                Path(args[args.index("-o") + 1]).write_text("last codex message")

        def communicate(self, **kwargs):
            assert kwargs == {"input": b"approved prompt", "timeout": 21}

        def poll(self):
            return self.returncode

    monkeypatch.setattr("foragent.processes.subprocess.Popen", Process)
    result = executor_for(kind).run(
        worktree=tmp_path, prompt="approved prompt", model="chosen", timeout_s=21, log_path=log,
    )
    assert result.exit_code == 7 and not result.timed_out and result.log_path == log
    assert result.last_message == ("last codex message" if kind == "codex" else "session stdout\n")
    assert log.read_text().startswith("earlier\n") and "session stdout" in log.read_text()
    args, kwargs = calls[0]
    assert kwargs["cwd"] == tmp_path and kwargs["stdin"] == subprocess.PIPE
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["PATH"] == os.pathsep.join([str(project_bin), "/usr/bin"])
    assert kwargs["env"]["FORAGENT_TEST_KEEP"] == "kept"
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        assert key not in kwargs["env"]
        assert os.environ[key] == str(gate_env)
    if kind == "claude":
        assert "session stderr" in log.read_text()
    else:
        assert not Path(args[args.index("-o") + 1]).exists()


def test_logged_process_really_runs_offline_and_appends_both_streams(tmp_path):
    log = tmp_path / "session.log"
    log.write_text("old\n")
    code, timed_out, tail = run_logged(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err', file=sys.stderr)"],
        cwd=tmp_path, timeout_s=5, log_path=log, prompt="hello", capture_stdout=True,
    )
    assert code == 0 and not timed_out and tail == "hello\n"
    assert log.read_text() == "old\nerr\nhello\n"


def test_done_when_child_uses_clean_environment_and_project_path(tmp_path, monkeypatch):
    from foragent.runner import _check

    gate_env = tmp_path / "gate/.venv"
    project_bin = tmp_path / "project/.venv/bin"
    project_bin.mkdir(parents=True)
    probe = project_bin / "project-probe"
    probe.write_text('#!/bin/sh\nprintf "project environment\\n"\n')
    probe.chmod(0o755)
    script = tmp_path / "inspect_env.py"
    script.write_text(
        "import json, os\n"
        "keys = ['PATH', 'VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', 'PYTHONHOME', 'UV_PROJECT_ENVIRONMENT']\n"
        "print(json.dumps({key: os.environ[key] for key in keys if key in os.environ}))\n"
    )
    monkeypatch.setattr(sys, "prefix", str(gate_env))
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.setenv(key, str(gate_env))
    path = os.pathsep.join([str(gate_env / "bin"), str(gate_env / "bin") + "/", str(project_bin), "/usr/bin"])
    monkeypatch.setenv("PATH", path)
    from shlex import quote

    result = _check(
        f"{quote(sys.executable)} inspect_env.py && project-probe", tmp_path, tmp_path / "check.log", 5,
    )
    assert result.exit_code == 0 and not result.timed_out
    child_env = json.loads(result.output.splitlines()[0])
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        assert key not in child_env
    assert child_env["PATH"] == os.pathsep.join([str(project_bin), "/usr/bin"])
    assert "project environment" in result.output
    assert os.environ["PATH"] == path


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("already_gone", [False, True])
def test_timeout_kills_whole_process_group(tmp_path, monkeypatch, capture, already_gone):
    calls = []

    class Process:
        pid = 54321
        returncode = None

        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"]

        def communicate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("offline child", 1)
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr("foragent.processes.subprocess.Popen", Process)
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        if already_gone:
            raise ProcessLookupError

    monkeypatch.setattr("foragent.processes.os.killpg", kill)
    code, timed_out, _ = run_logged(
        ["offline child"], cwd=tmp_path, timeout_s=1, log_path=tmp_path / "log", capture_stdout=capture,
    )
    assert timed_out and code == -9 and killed == [(54321, signal.SIGKILL)]
    assert len(calls) == 2


def test_termination_signal_kills_executor_group_and_restores_handler(tmp_path, monkeypatch):
    calls = []
    handlers = []

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, **kwargs):
            handlers[0](signal.SIGTERM, None)

        def poll(self):
            return self.returncode

        def wait(self):
            calls.append("wait")
            self.returncode = -9

    def install(sig, handler):
        assert sig == signal.SIGTERM
        handlers.append(handler)
        return "previous handler"

    monkeypatch.setattr("foragent.processes.subprocess.Popen", Process)
    monkeypatch.setattr("foragent.processes.signal.signal", install)
    monkeypatch.setattr("foragent.processes.os.killpg", lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(SystemExit) as exc:
        run_logged(["offline child"], cwd=tmp_path, timeout_s=3, log_path=tmp_path / "log")
    assert exc.value.code == 128 + signal.SIGTERM
    assert calls == [(12345, signal.SIGKILL), "wait"]
    assert handlers[-1] == "previous handler"


@pytest.mark.parametrize("alive", [False, True])
def test_tick_group_kill_escalates_after_ten_seconds(monkeypatch, alive):
    calls = []
    running = [alive]

    def killpg(pid, sig):
        calls.append((pid, sig))
        if sig == signal.SIGKILL:
            running[0] = False
        if sig == 0 and not running[0]:
            raise ProcessLookupError

    monkeypatch.setattr("foragent.processes.os.killpg", killpg)
    monkeypatch.setattr("foragent.processes.os.waitpid", lambda *args: (0, 0))
    monkeypatch.setattr("foragent.processes.time.sleep", lambda seconds: calls.append(("sleep", seconds)))
    kill_process_group(321)
    assert calls == [(321, signal.SIGTERM), ("sleep", 10), (321, 0)] + (
        [(321, signal.SIGKILL), (321, 0)] if alive else []
    )


def test_forced_termination_waits_for_confirmed_exit(monkeypatch):
    probes = iter([True, True, False])
    waits = []
    reaped = []

    def killpg(pid, sig):
        if sig == 0 and not next(probes):
            raise ProcessLookupError

    monkeypatch.setattr("foragent.processes.os.killpg", killpg)
    monkeypatch.setattr("foragent.processes.os.waitpid", lambda *args: reaped.append(args))
    monkeypatch.setattr("foragent.processes.time.sleep", waits.append)
    kill_process_group(321)
    assert waits == [10, 0.1]
    assert reaped == [(321, os.WNOHANG)] * 3


def test_unconfirmed_forced_termination_raises(monkeypatch):
    monkeypatch.setattr("foragent.processes.os.killpg", lambda *args: None)

    def not_child(*args):
        raise ChildProcessError

    monkeypatch.setattr("foragent.processes.os.waitpid", not_child)
    monkeypatch.setattr("foragent.processes.time.sleep", lambda seconds: None)
    with pytest.raises(TimeoutError, match="did not terminate"):
        kill_process_group(321)


def test_tick_group_already_gone(monkeypatch):
    monkeypatch.setattr("foragent.processes.os.killpg", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr("foragent.processes.time.sleep", lambda seconds: pytest.fail("must not wait"))
    kill_process_group(321)

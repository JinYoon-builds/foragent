from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from foragent.executors import (
    ClaudeExecutor, CodexExecutor, ExecResult, classify_result, claude_command, claude_review_command, codex_command, executor_for,
)
from foragent.processes import kill_process_group, run_logged


@pytest.mark.parametrize("model", [None, "model with spaces"])
def test_exact_codex_command(model):
    assert codex_command(Path("/work tree"), Path("/last message"), model) == [
        "codex", "exec", "-", "-C", "/work tree", "-s", "workspace-write",
        "--skip-git-repo-check", "--json", "-o", "/last message",
        *(["-m", model] if model is not None else []),
    ]


def test_codex_effort_is_an_explicit_config_argument(tmp_path, monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        kwargs["stdout_path"].write_text("")
        return 0, False, ""

    monkeypatch.setattr("foragent.executors.run_logged", execute)
    CodexExecutor().run(worktree=tmp_path, prompt="implement", model="lane-model", effort="xhigh",
                        timeout_s=10, log_path=tmp_path / "run.log")
    assert calls[0][-4:] == ["-m", "lane-model", "-c", "model_reasoning_effort=xhigh"]


@pytest.mark.parametrize("model", [None, "other model"])
def test_exact_claude_command(model):
    assert claude_command(model, Path("/run dir/claude-settings.json")) == [
        "claude", "-p", "--permission-mode", "auto", "--output-format", "stream-json", "--verbose",
        "--settings", "/run dir/claude-settings.json",
        *(["--model", model] if model is not None else []),
    ]


def test_executor_factory():
    assert isinstance(executor_for("codex"), CodexExecutor)
    assert isinstance(executor_for("claude"), ClaudeExecutor)
    with pytest.raises(ValueError, match="unknown executor"):
        executor_for("unknown")


@pytest.mark.parametrize("message", [
    "Tests failed: expected quota=10, got quota=0", "Implemented quota accounting",
    "VERDICT: FAIL\nfeature.py:503: rate limit handling is broken", "503 overloaded usage limit quota",
])
@pytest.mark.parametrize("code", [0, 1, 429, 503, 529, None])
def test_free_text_and_exit_codes_never_classify_availability(tmp_path, message, code):
    result = ExecResult(code, False, message, tmp_path / "log", message, message)
    assert classify_result(result) == "normal"


def execute_events(tmp_path, monkeypatch, executor, events, *, code=1, review=False):
    log = tmp_path / "run.log"
    log.write_text('{"type":"error","message":"HTTP 429"}\n')
    output = json.dumps(events, indent=2) if review else "\n".join(json.dumps(event) for event in events)

    def execute(args, **kwargs):
        kwargs["stdout_path"].write_text(output + "\n")
        with kwargs["log_path"].open("a") as stream:
            stream.write("Tests failed: expected quota=10, got quota=0\nHTTP 429\n")
        if executor == "codex":
            Path(args[args.index("-o") + 1]).write_text("Implemented quota accounting")
        return code, False, ""

    monkeypatch.setattr("foragent.executors.run_logged", execute)
    return executor_for(executor).run(
        worktree=tmp_path, prompt="implement", model=None, timeout_s=60, log_path=log,
        **({"review_level": "high"} if review else {}),
    )


@pytest.mark.parametrize("event,expected", [
    ({"type": "error", "message": "HTTP 429: usage limit"}, "limit"),
    ({"type": "turn.failed", "error": {"message": "rate limit exceeded"}}, "limit"),
    ({"type": "turn.failed", "message": "HTTP 503 Service Unavailable"}, "transient"),
    ({"type": "error", "message": "overloaded (529)"}, "transient"),
    ({"type": "error", "message": "stream disconnected: connection reset"}, "transient"),
    ({"type": "error", "message": "quota exceeded"}, "limit"),
    ({"type": "error", "message": "failed test at line 5030"}, "normal"),
    ({"type": "item.completed", "item": {"type": "agent_message", "text": "HTTP 429 quota"}}, "normal"),
    ({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output":
      '{"type":"error","message":"HTTP 429"}'}}, "normal"),
    ({"type": "turn.completed", "message": "quota exceeded"}, "normal"),
    ({"type": "result", "is_error": True, "api_error_status": 429}, "normal"),
    ({"type": "error", "result": "HTTP 429"}, "normal"),
    ({"type": "turn.failed", "error": {"type": "rate_limit_error", "message": "unrelated failure"}}, "normal"),
    ({"type": [], "message": "HTTP 429"}, "normal"),
    ([], "normal"), ("HTTP 429", "normal"),
])
@pytest.mark.parametrize("code", [0, 1])
def test_codex_classifies_only_structured_error_messages(tmp_path, monkeypatch, event, expected, code):
    result = execute_events(tmp_path, monkeypatch, "codex", [event], code=code)
    assert classify_result(result) == (expected if code else "normal")


@pytest.mark.parametrize("event,expected", [
    ({"type": "result", "is_error": True, "api_error_status": 429}, "limit"),
    ({"type": "result", "is_error": True, "api_error_status": "429"}, "limit"),
    ({"type": "result", "is_error": True, "api_error_status": 503}, "transient"),
    ({"type": "result", "is_error": True, "api_error_status": 529}, "transient"),
    ({"type": "result", "is_error": True, "api_error_status": "overloaded"}, "transient"),
    ({"type": "result", "is_error": False, "api_error_status": 429}, "normal"),
    ({"type": "result", "api_error_status": 429}, "normal"),
    ({"type": "result", "is_error": True, "result": "Tests failed: expected quota=10; HTTP 429"}, "normal"),
    ({"type": "result", "is_error": False, "result": "Implemented quota accounting"}, "normal"),
    ({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "transient"),
    ({"type": "error", "api_error_status": 429}, "limit"),
    ({"type": "error", "message": "rate limit exceeded"}, "limit"),
    ({"type": "assistant", "message": "HTTP 429 quota", "api_error_status": 429}, "normal"),
    ({"type": "tool_result", "is_error": True, "api_error_status": 429}, "normal"),
])
@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("review", [False, True])
def test_claude_classifies_only_error_events_or_failed_result_status(tmp_path, monkeypatch, event, expected, code, review):
    result = execute_events(tmp_path, monkeypatch, "claude", event if review else [event], code=code, review=review)
    assert classify_result(result) == (expected if code else "normal")


def test_claude_review_json_without_type_uses_error_status(tmp_path, monkeypatch):
    result = execute_events(tmp_path, monkeypatch, "claude", {
        "is_error": True, "api_error_status": 529, "result": "review unavailable",
    }, review=True)
    assert classify_result(result) == "transient"


def test_structured_limits_take_precedence_over_transients(tmp_path, monkeypatch):
    result = execute_events(tmp_path, monkeypatch, "codex", [
        {"type": "error", "message": "HTTP 503"}, {"type": "error", "message": "HTTP 429"},
    ])
    assert classify_result(result) == "limit"


def test_claude_agent_preserves_native_arguments(tmp_path, monkeypatch):
    captured = []

    def execute(args, **kwargs):
        captured.append((args, kwargs))
        kwargs["stdout_path"].write_text('{"type":"result","result":"done"}\n')
        return 0, False, ""

    monkeypatch.setattr("foragent.executors.run_logged", execute)
    settings = tmp_path / "settings.json"
    ClaudeExecutor().run(
        worktree=tmp_path, prompt="/goal complete", model="lane-model", timeout_s=60,
        log_path=tmp_path / "run.log", settings_path=settings, agent="fa-ui",
    )
    args, kwargs = captured[0]
    assert args == [*claude_command(None, settings), "--agent", "fa-ui", "--model", "lane-model"]
    assert kwargs["prompt"] == "/goal complete"


@pytest.mark.parametrize("resume_session_id", [None, "saved-session"])
def test_claude_goal_has_its_own_turn_and_resume_only_sends_updates(tmp_path, monkeypatch, resume_session_id):
    calls = []
    goal = 'Every command in "Done when" exits 0.'
    prompt = 'Implement the migration for 연구 프로젝트.\n\nPreserve "saved settings".'

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        # A local slash-command result can precede the actual implementation.
        kwargs["stdout_path"].write_text("\n".join(json.dumps(event) for event in [
            {"type": "result", "num_turns": 0, "result": "Goal set"},
            {"type": "result", "num_turns": 2, "result": "Implemented"},
        ]) + "\n")
        return 0, False, ""

    monkeypatch.setattr("foragent.executors.run_logged", execute)
    result = ClaudeExecutor().run(
        worktree=tmp_path, prompt=prompt, goal=goal, model=None, timeout_s=60,
        log_path=tmp_path / "run.log", resume_session_id=resume_session_id,
    )
    args, kwargs = calls[0]
    if resume_session_id is None:
        assert args[-2:] == ["--input-format", "stream-json"]
        assert [json.loads(line) for line in kwargs["prompt"].splitlines()] == [
            {"type": "user", "message": {"role": "user", "content": content}}
            for content in (f"/goal {goal}", prompt)
        ]
    else:
        assert args == claude_command(resume_session_id=resume_session_id)
        assert kwargs["prompt"] == prompt
        assert goal not in kwargs["prompt"]
    assert result.last_message == "Implemented" and result.num_turns == 2


@pytest.mark.parametrize("model", [None, "review model"])
@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_exact_claude_review_command(level, model):
    assert claude_review_command(level, model) == [
        "claude", "-p", f"/code-review {level}", "--permission-mode", "auto", "--output-format", "json",
        *(["--model", model] if model is not None else []),
    ]


@pytest.mark.parametrize("pr", [None, 42])
def test_claude_review_uses_devnull_and_preserves_json(tmp_path, monkeypatch, pr):
    calls = []
    message = json.dumps({"result": "(none)", "is_error": False})

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            kwargs["stdout"].write(message.encode())

        def communicate(self, **kwargs):
            assert kwargs == {"input": None, "timeout": 12}

        def poll(self):
            return 0

    monkeypatch.setattr("foragent.processes.subprocess.Popen", Process)
    result = ClaudeExecutor().run(worktree=tmp_path, prompt="ignored", model=None, timeout_s=12,
                                  log_path=tmp_path / "review.log", review_level="high", review_pr=pr)
    expected_prompt = f"/code-review {pr} high" if pr is not None else "/code-review high"
    assert calls[0][0] == [
        "claude", "-p", expected_prompt, "--permission-mode", "auto", "--output-format", "json",
    ]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL and calls[0][1]["cwd"] == tmp_path
    assert result.last_message == result.full_message == message


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
            stdout = "session stdout\n" if kind == "codex" else json.dumps({"type": "result", "result": "session stdout\n"}) + "\n"
            kwargs["stdout"].write(stdout.encode())
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
        **({"settings_path": tmp_path / "claude-settings.json"} if kind == "claude" else {}),
    )
    assert result.exit_code == 7 and not result.timed_out and result.log_path == log
    assert result.last_message == ("last codex message" if kind == "codex" else "session stdout\n")
    assert result.full_message == result.last_message
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
        assert args == claude_command("chosen", tmp_path / "claude-settings.json")
        assert "session stderr" in log.read_text()
    else:
        assert not Path(args[args.index("-o") + 1]).exists()


def test_claude_extracts_last_full_result_separately_from_transcript_and_stderr(tmp_path, monkeypatch):
    message = "VERDICT: FAIL\n" + "\n".join(f"file.py:{i}: " + "x" * 930 for i in range(5)) + "\n"
    assert len(message) > 4708
    transcript = "\n".join([
        json.dumps({"type": "system", "message": "progress"}), "garbage", "[]",
        json.dumps({"type": "result", "result": "old result"}),
        json.dumps({"type": "result", "result": message}),
        json.dumps({"type": "assistant", "message": "later progress"}),
    ]) + "\n"
    monkeypatch.setattr("foragent.executors.claude_command", lambda model, settings: [
        sys.executable, "-c", f"import sys; sys.stdout.write({transcript!r}); sys.stderr.write('VERDICT: PASS\\n')",
    ])
    result = ClaudeExecutor().run(
        worktree=tmp_path, prompt="review", model=None, timeout_s=5, log_path=tmp_path / "review.log",
    )
    assert result.exit_code == 0 and not result.timed_out
    assert result.full_message == message
    assert result.last_message == message
    assert (tmp_path / "review.stdout").read_text() == transcript
    assert "VERDICT: PASS" in result.log_path.read_text() and "VERDICT: PASS" not in result.full_message


def test_logged_process_really_runs_offline_and_appends_both_streams(tmp_path):
    log = tmp_path / "session.log"
    log.write_text("old\n")
    code, timed_out, tail = run_logged(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err', file=sys.stderr)"],
        cwd=tmp_path, timeout_s=5, log_path=log, prompt="hello", capture_stdout=True,
    )
    assert code == 0 and not timed_out and tail == "hello\n"
    assert log.read_text() == "old\nerr\nhello\n"


def test_captured_stdout_reaches_log_before_executor_finishes(tmp_path):
    log = tmp_path / "session.log"
    release = tmp_path / "release"

    def observe_log():
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if log.exists() and "live progress" in log.read_text():
                release.touch()
                return
            time.sleep(0.01)

    observer = threading.Thread(target=observe_log)
    observer.start()
    try:
        code, timed_out, message = run_logged(
            [sys.executable, "-c",
             "from pathlib import Path\nimport time\nprint('live progress', flush=True)\n"
             "deadline = time.monotonic() + 4\n"
             "while not Path('release').exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
             "raise SystemExit(0 if Path('release').exists() else 1)"],
            cwd=tmp_path, timeout_s=6, log_path=log, capture_stdout=True,
        )
    finally:
        observer.join()
    assert code == 0 and not timed_out
    assert message == "live progress\n" and log.read_text().count("live progress") == 1


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

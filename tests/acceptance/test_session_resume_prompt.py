"""Acceptance test for session-resume-prompt. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest


# Contract: send and close stdin before process observation or callbacks, for
# fresh and resumed Claude/Codex sessions. Resume builders require a prompt;
# only fresh Claude sessions wrap /goal in stream-json. Failed resumes retain
# diagnostics and increment a persisted per-run count; new runs start at zero.
RETRY_HINT = 'Keep the selected locale "한국어" and finish the interrupted export.'
FOLLOW_UP = "Preserve the previous attempt's workspace selection.\nDo not discard its context."
BODY = (
    "<!-- foragent:v1 -->\n## Goal\nImplement the offline export.\n"
    "## Out of scope\nOther features.\n## Done when\n```sh\ntrue\n```\n"
)
CLI_ERRORS = {
    "claude": "Error: No deferred tool marker found in the resumed session…",
    "codex": "Error: saved Codex thread cannot be resumed: fixture transcript unavailable",
}
LAST_MESSAGE = "Resume failed: the saved transcript could not be loaded."
LAUNCH_ERROR = "Saved implementation session could not launch: offline executable unavailable"
STDIN_WINDOW_S = 0.5
TRACKING_DELAY_S = 1.0


FAKE_CLI = r'''
import argparse
import json
import os
from pathlib import Path
import select
import sys
import time

name = Path(sys.argv[0]).name
parser = argparse.ArgumentParser()
parser.add_argument("parts", nargs="*")
for option in ("-p", "--verbose", "--skip-git-repo-check", "--json"):
    parser.add_argument(option, action="store_true")
for option in ("--resume", "--permission-mode", "--output-format", "--settings",
               "--agent", "--model", "--input-format", "-m", "-o", "-C", "-s"):
    parser.add_argument(option)
parser.add_argument("-c", action="append")
options = parser.parse_intermixed_args()
parts = options.parts
resumed = options.resume is not None if name == "claude" else parts[:2] == ["exec", "resume"]
identity = options.resume if name == "claude" else parts[2] if resumed else None
positionals = parts if name == "claude" else parts[3:] if resumed else parts[1:]
positional_prompt = "\n".join(part for part in positionals if part != "-")
window = float(os.environ["FORAGENT_RESUME_WINDOW_S"])
Path(os.environ["FORAGENT_RESUME_READY"]).touch()
deadline = time.monotonic() + window
chunks, stdin_closed = [], False
while (remaining := deadline - time.monotonic()) > 0:
    if not select.select([sys.stdin.fileno()], [], [], remaining)[0]:
        break
    chunk = os.read(sys.stdin.fileno(), 65536)
    if not chunk:
        stdin_closed = True
        break
    chunks.append(chunk)
stdin = b"".join(chunks).decode("utf-8")
record = {
    "argv": [name, *sys.argv[1:]], "stdin": stdin, "positional_prompt": positional_prompt,
    "stdin_closed": stdin_closed, "stdin_window_s": window,
    "cwd": os.getcwd(), "resumed": resumed, "session_id": identity,
    "env": {key: os.environ.get(key) for key in (
        "PATH", "FORAGENT_RESUME_TEST", "FORAGENT_RESUME_CALLS", "FORAGENT_RESUME_MODE",
        "FORAGENT_RESUME_WINDOW_S",
    )},
}
with Path(os.environ["FORAGENT_RESUME_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
if not stdin_closed or not stdin.strip():
    print(f"Warning: no complete stdin received in {window}s, proceeding without it.", file=sys.stderr)
    print(os.environ["FORAGENT_RESUME_ERROR"], file=sys.stderr)
    raise SystemExit(1)
if resumed and os.environ["FORAGENT_RESUME_MODE"] == "exit-error":
    print(os.environ["FORAGENT_RESUME_ERROR"], file=sys.stderr)
    message = os.environ["FORAGENT_RESUME_LAST_MESSAGE"]
    print(json.dumps({"type": "result", "is_error": True, "result": message}
                     if name == "claude" else {"type": "error", "message": message}))
    raise SystemExit(1)
identity = identity if resumed else "offline-" + name + "-fresh"
if name == "claude":
    events = [
        {"type": "system", "subtype": "init", "session_id": identity},
        {"type": "result", "result": "Implemented", "num_turns": 1, "session_id": identity},
    ]
else:
    Path(options.o).write_text("Implemented\n", encoding="utf-8")
    events = [{"type": "thread.started", "thread_id": identity}, {"type": "turn.completed"}]
for event in events:
    print(json.dumps(event))
'''


@pytest.fixture
def fake_clis(tmp_path, monkeypatch):
    """Use real pipes and adapters, with a closed PATH containing only our CLIs."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("claude", "codex"):
        binary = directory / name
        binary.write_text(f"#!{sys.executable}\n" + FAKE_CLI, encoding="utf-8")
        binary.chmod(0o755)
    calls_path = tmp_path / "cli-calls.jsonl"
    ready_path = tmp_path / "stdin-ready"
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv("FORAGENT_RESUME_CALLS", str(calls_path))
    monkeypatch.setenv("FORAGENT_RESUME_READY", str(ready_path))
    monkeypatch.setenv("FORAGENT_RESUME_TEST", "offline-prompt-delivery")
    monkeypatch.setenv("FORAGENT_RESUME_MODE", "success")
    monkeypatch.setenv("FORAGENT_RESUME_LAST_MESSAGE", LAST_MESSAGE)
    monkeypatch.setenv("FORAGENT_RESUME_WINDOW_S", str(STDIN_WINDOW_S))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    popen = subprocess.Popen
    launches = []

    def only_fake_clis(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv, argv
        name = Path(argv[0]).name
        assert name in {"claude", "codex"}, f"Unexpected external command: {argv}"
        environment = kwargs.get("env", {})
        executable = shutil.which(str(argv[0]), path=environment.get("PATH", ""))
        assert executable is not None and Path(executable).resolve() == (directory / name).resolve(), (
            f"Attempted to launch a real model CLI: {argv}"
        )
        launches.append(list(argv))
        resumed = "--resume" in argv if name == "claude" else list(argv[1:3]) == ["exec", "resume"]
        if resumed and environment.get("FORAGENT_RESUME_MODE") == "launch-error":
            raise OSError(LAUNCH_ERROR)
        return popen(argv, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail("Session resume acceptance must remain offline")

    monkeypatch.setattr(subprocess, "Popen", only_fake_clis)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def calls():
        return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]

    return SimpleNamespace(directory=directory, calls_path=calls_path, ready_path=ready_path,
                           calls=calls, launches=launches)


def resume_case(tmp_path, monkeypatch, executor):
    """Exercise #153/#236's real session store and runner-to-executor boundary."""
    from foragent.config import Config
    from foragent.executors import executor_for
    from foragent.issue import parse_body
    from foragent.run_prompts import build_prompt, build_resume_prompt
    from foragent.state import StateStore

    clock = [datetime(2026, 10, 25, tzinfo=UTC)]
    worktree = tmp_path / "worktree with spaces"
    worktree.mkdir()
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    identity = "offline-" + executor + "-saved"
    store.start_run(1, branch="foragent/issue-1", worktree=worktree)
    store.update_run(1, session_id=identity, session_executor=executor,
                     session_ids=[identity], session_body=BODY)
    ctx = SimpleNamespace(
        store=store, config=Config(executor=executor, skill_libraries=[], max_runtime_minutes=1),
        now=lambda: clock[0], poll_hook=lambda execute, poll: execute(),
    )
    full_prompt = build_prompt(1, parse_body(BODY), "tests/acceptance/", FOLLOW_UP,
                               orchestrator_hint=RETRY_HINT)
    continuation = build_resume_prompt(1, BODY, BODY, FOLLOW_UP, orchestrator_hint=RETRY_HINT)
    monkeypatch.setenv("FORAGENT_RESUME_ERROR", CLI_ERRORS[executor])
    return SimpleNamespace(ctx=ctx, store=store, executor=executor_for(executor),
                           worktree=worktree, identity=identity, full_prompt=full_prompt,
                           continuation=continuation, log=store.log_path(1), clock=clock)


def run_resume(case, continuation):
    from foragent.run_execution import _available_executor

    return _available_executor(
        1, case.ctx, case.executor, worktree=case.worktree, prompt=case.full_prompt,
        session_body=BODY, resume_prompt=continuation, model=None,
        timeout_s=60, log_path=case.log,
    )


def assert_cli_call(call, fake_clis, case, *, resumed, identity=None):
    argv = call["argv"]
    assert call["cwd"] == str(case.worktree)
    assert call["env"]["PATH"] == str(fake_clis.directory)
    assert call["env"]["FORAGENT_RESUME_TEST"] == "offline-prompt-delivery"
    assert call["env"]["FORAGENT_RESUME_CALLS"] == str(fake_clis.calls_path)
    assert call["env"]["FORAGENT_RESUME_WINDOW_S"] == str(STDIN_WINDOW_S)
    if argv[0] == "claude":
        assert argv[1] == "-p"
        assert ("--resume" in argv) is resumed
        if resumed:
            assert argv[argv.index("--resume") + 1] == identity
    else:
        assert argv[1] == "exec"
        assert (argv[2] == "resume") is resumed
        if resumed:
            assert argv[:4] == ["codex", "exec", "resume", identity]
    assert call["positional_prompt"] == "", "Implementation prompts must stay on stdin, not argv"
    received = call["stdin"]
    assert call["stdin_closed"], (
        f"{argv[0]} {'resumed' if resumed else 'fresh'} CLI did not receive closed stdin "
        f"within its {call['stdin_window_s']}s input window; deliver and close stdin "
        "before observing the process tree or running callbacks"
    )
    assert received.strip(), (
        f"{argv[0]} CLI received an empty prompt on stdin"
    )
    if "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json":
        received = "\n".join(json.loads(line)["message"]["content"] for line in received.splitlines())
    for text in (RETRY_HINT, FOLLOW_UP):
        assert text in received, f"{argv[0]} CLI lost continuation instructions: {text!r}"


@pytest.mark.parametrize("executor", ["claude", "codex"])
@pytest.mark.parametrize("resumed", [False, True], ids=["fresh", "resumed"])
@pytest.mark.parametrize("slow_seam", ["observe_tree", "process_group", "process_tree"])
def test_session_prompt_stdin_closes_before_slow_observation_or_callbacks(
    tmp_path, monkeypatch, fake_clis, executor, resumed, slow_seam,
):
    from foragent import processes
    from foragent.run_prompts import goal_condition

    case = resume_case(tmp_path, monkeypatch, executor)
    observed = processes.observe_tree
    delayed = []

    def delay(seam):
        if seam == slow_seam and not delayed:
            delayed.append(seam)
            # Start the delay after the child is ready, so scheduler load cannot
            # let a late-starting fake miss the intended observation/stdio race.
            deadline = time.monotonic() + 5
            while not fake_clis.ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            assert fake_clis.ready_path.exists(), "Fake CLI did not start its stdin reader"
            time.sleep(TRACKING_DELAY_S)

    def observe_tree(identities=(), roots=()):
        if roots:
            delay("observe_tree")
        return observed(identities, roots)

    def process_group(pid):
        if pid is not None:
            delay("process_group")

    def process_tree(identities):
        delay("process_tree")

    monkeypatch.setattr(processes, "observe_tree", observe_tree)
    goal = goal_condition("tests/acceptance/")
    options = {"goal": goal} if executor == "claude" else {}
    if resumed:
        options["resume_session_id"] = case.identity
    prompt = case.continuation if resumed else case.full_prompt
    assert prompt.strip(), "Reproduce delayed delivery of a real, non-empty runner prompt"
    # The same goal is supplied on resume to prove it is not wrapped a second time.
    with processes.track_process_group(process_group, tree_callback=process_tree):
        result = case.executor.run(
            worktree=case.worktree, prompt=prompt, model=None,
            timeout_s=60, log_path=case.log, **options,
        )
    assert delayed == [slow_seam], "The deliberately slow production tracking seam was not exercised"
    call, = fake_clis.calls()
    assert_cli_call(call, fake_clis, case, resumed=resumed, identity=case.identity)
    if executor == "claude" and not resumed:
        argv = call["argv"]
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert [json.loads(line) for line in call["stdin"].splitlines()] == [
            {"type": "user", "message": {"role": "user", "content": text}}
            for text in (f"/goal {goal}", prompt)
        ]
    else:
        assert "--input-format" not in call["argv"]
        assert call["stdin"] == prompt
        assert "/goal " not in call["stdin"]
    assert result.exit_code == 0 and not result.timed_out
    assert result.session_id == (case.identity if resumed else "offline-" + executor + "-fresh")


@pytest.mark.parametrize("executor", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["exit-error", "launch-error"])
def test_resume_failure_logs_cli_error_counts_each_failure_and_still_starts_fresh(
    tmp_path, monkeypatch, fake_clis, executor, failure,
):
    from foragent.state import StateStore

    case = resume_case(tmp_path, monkeypatch, executor)
    initial_count = getattr(case.store.get_run(1), "resume_failures", None)
    monkeypatch.setenv("FORAGENT_RESUME_MODE", failure)
    counts = []
    for attempt in range(2):
        identity = case.store.get_run(1).session_id
        before = len(fake_clis.calls()) if fake_clis.calls_path.exists() else 0
        result = run_resume(case, case.continuation)
        assert result.exit_code == 0 and not result.timed_out
        assert len(fake_clis.launches) == (attempt + 1) * 2
        attempted = fake_clis.launches[attempt * 2]
        if executor == "claude":
            assert attempted[attempted.index("--resume") + 1] == identity
        else:
            assert attempted[:4] == ["codex", "exec", "resume", identity]
        calls = fake_clis.calls()[before:]
        if failure == "exit-error":
            resumed, fresh = calls
            assert_cli_call(resumed, fake_clis, case, resumed=True, identity=identity)
            assert BODY not in resumed["stdin"]
        else:
            fresh, = calls  # Popen raised before a resumed executable could run.
        assert_cli_call(fresh, fake_clis, case, resumed=False)
        assert "Implement the offline export." in fresh["stdin"]
        log = case.log.read_text(encoding="utf-8")
        marker = ("Session resume failed; starting fresh" if failure == "exit-error" else
                  "Session resume could not launch; starting fresh")
        assert log.count(marker) == attempt + 1
        diagnostics = log.rsplit(marker, 1)[1]
        if failure == "exit-error":
            assert LAST_MESSAGE in diagnostics, "Resume fallback omitted the CLI's last_message"
            assert CLI_ERRORS[executor] in diagnostics, "Resume fallback omitted the CLI's stderr log tail"
        else:
            assert LAUNCH_ERROR in diagnostics, "Resume fallback hid the launch exception text"
        # Reload between attempts: a process-local counter cannot feed #333 facts.
        case.store = StateStore(case.store.root, now=case.ctx.now)
        case.ctx.store = case.store
        run = case.store.get_run(1)
        assert run.session_id == result.session_id != case.identity
        counts.append(getattr(run, "resume_failures", None))
    monkeypatch.setenv("FORAGENT_RESUME_MODE", "success")
    result = run_resume(case, case.continuation)
    assert result.exit_code == 0
    assert_cli_call(fake_clis.calls()[-1], fake_clis, case, resumed=True, identity=result.session_id)
    after_success = getattr(StateStore(case.store.root).get_run(1), "resume_failures", None)
    case.store.finish_run(1, outcome="completed")
    history, = StateStore(case.store.root).read_history()
    # A new execution of the same issue retains the session but resets this metric.
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.worktree)
    new_count = getattr(StateStore(case.store.root).get_run(1), "resume_failures", None)
    assert counts == [1, 2], (
        f"Run record must persist every failed resume for facts; expected [1, 2], got {counts}"
    )
    assert initial_count == 0 and new_count == 0, "Each new run record must start with zero resume failures"
    assert after_success == 2, "A successful resume must not increment resume_failures"
    assert getattr(history, "resume_failures", None) == 2, "Run history lost the resume failure count"


@pytest.mark.parametrize("executor", ["claude", "codex"])
def test_resume_command_builder_refuses_missing_empty_and_whitespace_prompt(tmp_path, executor):
    from foragent.executors import claude_command, codex_command

    def command(**options):
        if executor == "claude":
            return claude_command(resume_session_id="saved-session", **options)
        return codex_command(tmp_path, tmp_path / "last-message.txt",
                             resume_session_id="saved-session", **options)

    # A builder that cannot see the prompt cannot enforce this invariant.
    with pytest.raises((ValueError, TypeError)):
        command()
    for empty in (None, "", " \n\t"):
        with pytest.raises(ValueError):
            command(prompt=empty)
    # Rejecting all resumes is not a valid implementation of the guard.
    argv = command(prompt=RETRY_HINT)
    assert all(RETRY_HINT not in argument for argument in argv), "Prompt validation must not move stdin to argv"
    if executor == "claude":
        assert argv[argv.index("--resume") + 1] == "saved-session"
    else:
        assert argv[:4] == ["codex", "exec", "resume", "saved-session"]

"""Acceptance test for claude-sessions. Locked by Foragent; do not edit."""

import json
from pathlib import Path
import subprocess

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


SESSION_ID = "offline-claude-issue-1"
THREAD_ID = "offline-codex-issue-1"


@pytest.fixture(autouse=True)
def sessions_offline(isolated_home, tmp_path, monkeypatch):
    """Keep session, talk and click tests away from accounts and real terminals."""
    import curses
    import socket
    import tempfile

    def forbidden(*args, **kwargs):
        pytest.fail("Session acceptance must not open a real terminal or network connection")

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(curses, "initscr", forbidden)
    monkeypatch.setattr(curses, "wrapper", forbidden)
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, (
            f"An external command escaped the fake runner: {argv}"
        )
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


class SessionCommands:
    """Supply executor JSON at the process boundary; persistence stays real."""

    def __init__(self, case, monkeypatch, attempts):
        from foragent import executors

        self.case, self.attempts, self.calls = case, list(attempts), []
        monkeypatch.setattr(executors, "run_logged", self.run)
        case.ctx.executor_factory = lambda name: (
            executors.executor_for(name) if name == case.config.executor else case.reviewer
        )
        case.ctx.poll_hook = lambda execute, poll: execute()

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert "--bg" not in argv, "D55 keeps codex exec / claude -p workers"
        executor = self.case.config.executor
        assert Path(argv[0]).name == executor
        assert argv[1] == ("exec" if executor == "codex" else "-p"), argv
        assert self.attempts, "Unexpected implementation attempt"
        events = self.attempts.pop(0)
        message = f"Implemented attempt {len(self.calls)}"
        worktree = Path(kwargs["cwd"])
        (worktree / "feature.txt").write_text(message + "\n", encoding="utf-8")
        output = "\n".join(json.dumps(event) for event in events) + "\n"
        stdout = Path(kwargs["stdout_path"])
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text(output, encoding="utf-8")
        log = Path(kwargs["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(output)
        if executor == "codex":
            Path(argv[argv.index("-o") + 1]).write_text(message, encoding="utf-8")
        return 0, False, output


def session_case(git_history, tmp_path, monkeypatch, *, executor, attempts):
    reviewer = "claude" if executor == "codex" else "codex"
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor, reviewer=reviewer,
                     skill_libraries=[], test_command="true")
    if reviewer == "claude":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    gh = record_gh(monkeypatch)
    return case, SessionCommands(case, monkeypatch, attempts), gh


def recorded_session(case, session_id, executor, session_ids):
    from foragent.state import StateStore

    # Reload from disk, including history, rather than inspecting an adapter result.
    store = StateStore(case.store.root)
    run = store.get_run(1)
    assert run.session_id == session_id
    assert run.session_executor == executor
    assert run.session_ids == session_ids
    history = [past for past in store.read_history() if past.number == 1]
    assert len(history) == 1
    assert history[0].session_id == session_id
    assert history[0].session_executor == executor
    assert history[0].session_ids == session_ids
    assert run.status == "finished" and run.outcome == "merged"


def test_claude_sessions_codex_exec_records_thread_id_and_keeps_it_in_history(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    events = [{"type": "thread.started", "thread_id": THREAD_ID}, {"type": "turn.completed"}]
    case, commands, gh = session_case(git_history, tmp_path, monkeypatch,
                                      executor="codex", attempts=[events])
    assert run_issue(1, case.ctx) == "merged"
    assert len(commands.calls) == 1
    assert commands.calls[0][:2] == ["codex", "exec"]
    assert "--json" in commands.calls[0] and "--bg" not in commands.calls[0]
    recorded_session(case, THREAD_ID, "codex", [THREAD_ID])
    assert gh == []


def test_claude_sessions_headless_claude_records_init_session_id_and_history(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    events = [{"type": "system", "subtype": "init", "session_id": SESSION_ID},
              {"type": "result", "result": "Implemented"}]
    case, commands, gh = session_case(git_history, tmp_path, monkeypatch,
                                      executor="claude", attempts=[events])
    assert run_issue(1, case.ctx) == "merged"
    assert len(commands.calls) == 1
    argv = commands.calls[0]
    assert argv[:2] == ["claude", "-p"] and "--bg" not in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    recorded_session(case, SESSION_ID, "claude", [SESSION_ID])
    assert gh == []


def test_claude_sessions_result_event_can_supply_claude_session_id_without_init_id(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    events = [{"type": "system", "subtype": "init"},
              {"type": "result", "result": "Implemented", "session_id": SESSION_ID}]
    case, _, gh = session_case(git_history, tmp_path, monkeypatch,
                               executor="claude", attempts=[events])
    assert run_issue(1, case.ctx) == "merged"
    recorded_session(case, SESSION_ID, "claude", [SESSION_ID])
    assert gh == []


def test_claude_sessions_retry_replaces_current_id_and_preserves_attempt_ids_oldest_first(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    completed = []
    for executor in ("codex", "claude"):
        with monkeypatch.context() as patch:
            identity = THREAD_ID if executor == "codex" else SESSION_ID
            ids = [identity, identity + "-retry"]
            attempts = [[{"type": "thread.started", "thread_id": value},
                         {"type": "turn.completed"}] if executor == "codex" else [
                             {"type": "system", "subtype": "init", "session_id": value},
                             {"type": "result", "result": "Implemented", "session_id": value},
                         ] for value in ids]
            case, commands, gh = session_case(git_history, tmp_path / executor, patch,
                                              executor=executor, attempts=attempts)
            case.reviewer.steps = [json.dumps({"result": "feature.txt:1 preserve the newline"}),
                                   json.dumps({"result": "(none)"})] if executor == "codex" else [
                                       "VERDICT: FAIL\nfeature.txt:1 preserve the newline", "VERDICT: PASS",
                                   ]
            assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
            assert len(commands.calls) == 2 and case.store.get_run(1).attempts == 2
            assert gh == []
            completed.append((case, executor, ids))
    for case, executor, ids in completed:
        recorded_session(case, ids[-1], executor, ids)


def test_claude_sessions_output_without_id_keeps_successful_run_and_records_none(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    completed = []
    for executor in ("codex", "claude"):
        with monkeypatch.context() as patch:
            events = [{"type": "turn.completed"}] if executor == "codex" else [
                {"type": "system", "subtype": "init"}, {"type": "result", "result": "Implemented"},
            ]
            case, commands, gh = session_case(git_history, tmp_path / executor, patch,
                                              executor=executor, attempts=[events])
            assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
            assert case.tracker.get_issue(1).state == "closed" and len(commands.calls) == 1
            assert gh == []
            completed.append((case, executor))
    for case, executor in completed:
        recorded_session(case, None, executor, [])


# Kept from the earlier version so running baselines still find this test name (D53).
from datetime import datetime, timedelta
from test_orch_review_handoff import make_case, tick_context


SESSION_ID = "offline-claude-issue-1"


class _OldSessionCommands:
    """Fake only command I/O; keep the executor, runner and result reader real."""

    def __init__(self, case, monkeypatch):
        from foragent import executors

        self.case, self.calls, self.facts, self.recorded_ids = case, [], [], []
        self.progress = []
        self.worktree = self.output = None
        original = subprocess.run
        original_popen = subprocess.Popen

        def command(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name == "claude":
                output = self.reply(list(argv), kwargs)
                return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
            return original(argv, *args, **kwargs)

        def logged(argv, **kwargs):
            return 0, False, self.reply(list(argv), kwargs)

        def popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name in {"claude", "codex", "orca", "tmux"}:
                pytest.fail(f"Executor escaped the fake command runner: {argv}")
            return original_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", command)
        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(executors, "run_logged", logged)
        case.ctx.executor_factory = lambda name: (
            executors.executor_for(name) if name == case.config.executor else case.reviewer
        )
        # No watcher threads or wall-clock sleeps are needed with command facts.
        case.ctx.poll_hook = lambda execute, poll: execute()

    def complete(self):
        self.case.implement({"worktree": self.worktree})
        result = json.dumps({"type": "result", "result": "Named session result"}) + "\n"
        self.output.write_text(result, encoding="utf-8")
        log = self.case.store.log_path(1)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(result)

    def reply(self, argv, kwargs):
        self.calls.append(argv)
        if argv[1:2] == ["agents"]:
            assert "--json" in argv and "--all" in argv, argv
            assert len(self.facts) < 2, "The executor ignored its matching done fact"
            self.recorded_ids.append(getattr(self.case.store.get_run(1), "session_id", None))
            self.progress.append(self.case.store.get_run(1).last_progress_at)
            self.case.clock[0] += timedelta(seconds=45)
            status = "running" if not self.facts else "done"
            self.facts.append(status)
            if status == "done":
                self.complete()
            return json.dumps([
                {"id": "another-session", "name": "#9 Other work",
                 "status": "done" if status == "running" else "running"},
                {"id": SESSION_ID, "name": "#1 Implement feature", "status": status},
            ])
        if argv[1:2] == ["rm"]:
            assert argv == ["claude", "rm", SESSION_ID], argv
            return ""
        assert Path(argv[0]).name in {"claude", "codex"}, argv
        self.worktree = Path(kwargs["cwd"])
        self.output = Path(kwargs["stdout_path"])
        self.output.parent.mkdir(parents=True, exist_ok=True)
        if Path(argv[0]).name == "codex":
            self.case.implement({"worktree": self.worktree})
            Path(argv[argv.index("-o") + 1]).write_text("Codex result", encoding="utf-8")
            self.output.write_text('{}\n', encoding="utf-8")
            return ""
        if "--bg" in argv:
            output = json.dumps({"type": "system", "session_id": SESSION_ID, "id": SESSION_ID}) + "\n"
            self.output.write_text(output, encoding="utf-8")
            return output
        # Let today's headless execution complete so cleanup tests reach cleanup.
        self.complete()
        return self.output.read_text(encoding="utf-8")


def _old_session_case(git_history, tmp_path, monkeypatch, **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", **config)
    if case.config.reviewer == "claude":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    record_gh(monkeypatch)
    return case, _OldSessionCommands(case, monkeypatch)


def test_claude_sessions_codex_lane_keeps_exec_argv(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case, commands = _old_session_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude")
    assert run_issue(1, case.ctx) == "merged"
    argv = commands.calls[0]
    assert argv[:4] == ["codex", "exec", "-", "-C"]
    assert argv[4] == str(case.store.worktree_path(1))
    assert argv[5:10] == ["-s", "workspace-write", "--skip-git-repo-check", "--json", "-o"]
    assert argv[12:] == ["model_reasoning_effort=high"] and argv[11] == "-c"
    assert not any(Path(call[0]).name == "claude" for call in commands.calls)

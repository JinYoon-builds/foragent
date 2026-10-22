"""Acceptance test for claude-sessions. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home


SESSION_ID = "offline-claude-issue-1"


class SessionCommands:
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


def session_case(git_history, tmp_path, monkeypatch, **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", **config)
    if case.config.reviewer == "claude":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    record_gh(monkeypatch)
    return case, SessionCommands(case, monkeypatch)


def test_claude_sessions_named_background_run_waits_for_its_own_done_fact(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    case, commands = session_case(git_history, tmp_path, monkeypatch)
    outcome = run_issue(1, case.ctx)
    argv = commands.calls[0]
    assert argv[:2] == ["claude", "--bg"], argv
    assert argv[argv.index("-n") + 1] == "#1 Implement feature"
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1]
    assert commands.facts == ["running", "done"], "Launch success or another session's done is not completion"
    assert commands.recorded_ids == [SESSION_ID, SESSION_ID], "Persist the id before polling"
    assert datetime.fromisoformat(commands.progress[1]) > datetime.fromisoformat(commands.progress[0]), (
        "The matching running fact must report progress even before any result/log output"
    )
    run = StateStore(case.store.root).get_run(1)
    assert run.session_id == SESSION_ID and run.status == "finished" and outcome == "merged"
    assert "Named session result" in case.store.log_path(1).with_suffix(".stdout").read_text()


def test_claude_sessions_removes_session_when_issue_is_merged(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case, commands = session_case(git_history, tmp_path, monkeypatch)
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert case.tracker.get_issue(1).state == "closed"
    assert [argv for argv in commands.calls if argv[1:2] == ["rm"]] == [["claude", "rm", SESSION_ID]]


def test_claude_sessions_removes_session_when_handed_to_a_person(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case, commands = session_case(git_history, tmp_path, monkeypatch, review="careful")
    assert run_issue(1, case.ctx) == "pr-open"
    assert case.tracker.created_prs and not case.tracker.merged_prs
    assert [argv for argv in commands.calls if argv[1:2] == ["rm"]] == [["claude", "rm", SESSION_ID]]


def test_claude_sessions_recovery_removes_dead_runners_recorded_session(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, commands = session_case(git_history, tmp_path, monkeypatch)
    case.store.update_run(1, session_id=SESSION_ID)
    case.clock[0] += timedelta(minutes=3)
    ctx = tick_context(case, start_runs=False)
    ctx.runner_process_exists = lambda number: False
    report = tick(ctx)
    assert any("#1 runner crashed" in action for action in report.actions), report.actions
    assert [argv for argv in commands.calls if argv[1:2] == ["rm"]] == [["claude", "rm", SESSION_ID]]
    assert case.store.get_run(1).outcome == "crashed"


def test_claude_sessions_codex_lane_keeps_exec_argv(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case, commands = session_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude")
    assert run_issue(1, case.ctx) == "merged"
    argv = commands.calls[0]
    assert argv[:4] == ["codex", "exec", "-", "-C"]
    assert argv[4] == str(case.store.worktree_path(1))
    assert argv[5:10] == ["-s", "workspace-write", "--skip-git-repo-check", "--json", "-o"]
    assert argv[12:] == ["model_reasoning_effort=high"] and argv[11] == "-c"
    assert not any(Path(call[0]).name == "claude" for call in commands.calls)

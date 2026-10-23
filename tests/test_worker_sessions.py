"""Session continuity at executor, persistence and retry boundaries."""

from dataclasses import asdict, replace
from datetime import timedelta
import json
from pathlib import Path

import pytest

from foragent.executors import ExecResult, claude_command, codex_command, executor_for
from foragent.runner import ExecutorStopped, _available_executor, build_resume_prompt, run_issue
from foragent.state import StateStore
from test_executors import execute_events
from test_runner import BODY, run_case


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_session_identity_comes_from_lifecycle_events_only(tmp_path, monkeypatch, executor):
    lifecycle = ({"type": "thread.started", "thread_id": "actual"} if executor == "codex" else
                 {"type": "system", "subtype": "init", "session_id": "actual"})
    events = [
        {"type": "assistant", "session_id": "invented", "thread_id": "invented"},
        {"type": "item.completed", "item": lifecycle},
        {**lifecycle, "thread_id": [], "session_id": 123},
        lifecycle,
        {"type": "result", "result": "done", "session_id": "different-result-id"},
    ]
    result = execute_events(tmp_path, monkeypatch, executor, events, code=0)
    assert result.session_id == "actual"


def test_resume_commands_keep_headless_io_and_supported_options():
    codex = codex_command(Path("/work"), Path("/message"), "model", "high", resume_session_id="thread")
    assert codex[:5] == ["codex", "exec", "resume", "thread", "-"]
    assert "-C" not in codex and "-s" not in codex
    assert 'sandbox_mode="workspace-write"' in codex
    assert "--json" in codex and codex[codex.index("-o") + 1] == "/message"
    assert codex[-4:] == ["-m", "model", "-c", "model_reasoning_effort=high"]
    claude = claude_command("model", Path("/settings"), "fa-api", resume_session_id="session")
    assert claude[:2] == ["claude", "-p"]
    assert claude[claude.index("--resume") + 1] == "session"
    assert claude[claude.index("--agent") + 1] == "fa-api"


@pytest.mark.parametrize("outcome", ["spec-question", "timeout", "limit-wait", "crashed", "retries-exceeded"])
def test_new_run_preserves_session_snapshot_and_earlier_ids(tmp_path, outcome):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, session_id="current", session_executor="codex",
                     session_ids=["first", "current"], session_body=BODY)
    store.finish_run(1, outcome, "the concrete stop reason")
    restarted = StateStore(store.root).start_run(1, branch="branch", worktree="worktree")
    assert restarted.session_id == "current" and restarted.session_executor == "codex"
    assert restarted.session_ids == ["first", "current"] and restarted.session_body == BODY
    assert outcome in restarted.resume_reason and "the concrete stop reason" in restarted.resume_reason
    assert store.read_history()[0].session_ids == ["first", "current"]


def test_old_state_and_history_without_session_fields_remain_readable(tmp_path):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree="worktree")
    finished = asdict(store.finish_run(1, "merged"))
    for key in ("session_id", "session_executor", "session_ids", "session_body", "resume_reason"):
        finished.pop(key)
    (store.root / "runs/1.json").write_text(json.dumps(finished))
    (store.root / "runs.jsonl").write_text(json.dumps(finished) + "\n")
    for run in (store.get_run(1), *store.read_history()):
        assert run.session_id is run.session_executor is run.session_body is None
        assert run.session_ids == [] and run.resume_reason == ""


def test_resume_delta_reports_replacements_and_removals_without_repeating_context():
    before = "same context\nold requirement\nremove me\nmore context\n"
    after = "same context\nnew requirement\nmore context\n"
    prompt = build_resume_prompt(1, before, after, failure="check output", orchestrator_hint="answer")
    assert "new requirement" in prompt and "check output" in prompt and "answer" in prompt
    assert all(value not in prompt for value in ("same context", "old requirement", "remove me", "more context", "/goal"))
    assert "2-3" in prompt  # The replacement identifies the removed span as well.


def test_resume_delta_identifies_a_deleted_requirement_without_unchanged_lines():
    before = "keep this requirement\nno longer required\nkeep this too\n"
    after = "keep this requirement\nkeep this too\n"
    prompt = build_resume_prompt(1, before, after)
    assert "Remove these previous requirements:\nno longer required" in prompt
    assert "keep this requirement" not in prompt and "keep this too" not in prompt


def test_resume_that_started_but_failed_work_does_not_start_a_fresh_session(run_case):
    c = run_case
    c.store.update_run(1, session_id="worker", session_executor="codex", session_ids=["worker"])
    calls = []

    class Executor:
        def run(self, **kwargs):
            calls.append(kwargs)
            return ExecResult(1, False, "work failed", kwargs["log_path"], session_id="worker")

    result = _available_executor(
        1, c.ctx, Executor(), session_body=BODY, resume_prompt="continue",
        worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
    )
    assert len(calls) == 1 and result.exit_code == 1
    assert calls[0]["resume_session_id"] == "worker" and calls[0]["prompt"] == "continue"
    c.store.finish_run(1, "failed")
    c.tracker.remove_labels(1, ["running"])


def test_transient_retry_resumes_newly_recorded_session_with_error_output(run_case):
    c = run_case
    calls = []

    class Executor:
        def run(self, **kwargs):
            calls.append(kwargs)
            return ExecResult(1 if len(calls) == 1 else 0, False, "provider disconnected",
                              kwargs["log_path"], error_kind="transient" if len(calls) == 1 else "normal",
                              session_id="worker")

    result = _available_executor(
        1, c.ctx, Executor(), session_body=BODY, resume_prompt="continue",
        worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
    )
    assert result.exit_code == 0 and len(calls) == 2
    assert "resume_session_id" not in calls[0]
    assert calls[1]["resume_session_id"] == "worker"
    assert "provider disconnected" in calls[1]["prompt"] and "full prompt" not in calls[1]["prompt"]
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


def test_executor_change_does_not_resume_another_providers_id(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude")
    c.store.update_run(1, session_id="codex-thread", session_executor="codex", session_ids=["codex-thread"])
    calls = []

    class Executor:
        def run(self, **kwargs):
            calls.append(kwargs)
            return ExecResult(0, False, "done", kwargs["log_path"], session_id="claude-session")

    _available_executor(
        1, c.ctx, Executor(), session_body=BODY, resume_prompt="continue",
        worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
    )
    assert calls[0]["prompt"] == "full prompt" and "resume_session_id" not in calls[0]
    run = c.store.get_run(1)
    assert run.session_id == "claude-session" and run.session_executor == "claude"
    assert run.session_ids == ["codex-thread", "claude-session"]
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


@pytest.mark.parametrize("events,expected", [
    ([{"type": "result", "is_error": True, "num_turns": 0, "session_id": "worker"}], None),
    ([{"type": "result", "is_error": True, "num_turns": 1, "session_id": "worker"}], "worker"),
    ([{"type": "system", "subtype": "init", "session_id": "worker"},
      {"type": "result", "is_error": True, "num_turns": 0, "session_id": "worker"}], "worker"),
])
def test_claude_error_identity_requires_session_start(tmp_path, monkeypatch, events, expected):
    result = execute_events(tmp_path, monkeypatch, "claude", events)
    assert result.session_id == expected


def test_claude_rejected_resume_echoing_id_starts_fresh(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude")
    c.store.update_run(1, session_id="missing", session_executor="claude",
                       session_ids=["missing"], session_body=BODY)
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        events = ([{"type": "result", "is_error": True, "num_turns": 0,
                    "session_id": "missing", "result": "No conversation found"}] if len(calls) == 1 else
                  [{"type": "system", "subtype": "init", "session_id": "fresh"},
                   {"type": "result", "is_error": False, "num_turns": 1, "result": "done"}])
        kwargs["stdout_path"].write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return (1 if len(calls) == 1 else 0), False, ""

    monkeypatch.setattr("foragent.executors.run_logged", execute)
    try:
        result = _available_executor(
            1, c.ctx, executor_for("claude"), session_body=BODY, resume_prompt="continue",
            worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
        )
        assert len(calls) == 2 and result.exit_code == 0
        assert "--resume" in calls[0][0] and calls[0][1]["prompt"] == "continue"
        assert "--resume" not in calls[1][0] and calls[1][1]["prompt"] == "full prompt"
        run = StateStore(c.store.root).get_run(1)
        assert run.session_id == "fresh" and run.session_ids == ["missing", "fresh"]
    finally:
        c.store.finish_run(1, "failed")
        c.tracker.remove_labels(1, ["running"])


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_undelivered_spec_survives_limit_wait_and_restart(run_case, executor):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor=executor)
    updated_body = BODY.replace("Implement the approved feature.", "Implement Korean locale support.")
    c.store.update_run(1, session_id="worker", session_executor=executor,
                       session_ids=["worker"], session_body=BODY)
    calls = []

    class Executor:
        def run(self, **kwargs):
            calls.append(kwargs)
            return ExecResult(1, False, "quota exhausted before delivery", kwargs["log_path"], error_kind="limit")

    try:
        with pytest.raises(ExecutorStopped) as stopped:
            _available_executor(
                1, c.ctx, Executor(), session_body=updated_body,
                resume_prompt=build_resume_prompt(1, BODY, updated_body),
                worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
            )
        assert stopped.value.outcome == "limit-wait"
        c.store.finish_run(1, "limit-wait")
        c.now[0] += timedelta(seconds=1)
        reloaded = StateStore(c.store.root, now=c.ctx.now)
        run = reloaded.start_run(1, branch="branch", worktree=c.store.worktree_path(1))
        assert run.session_id == "worker" and run.session_body == BODY
        assert reloaded.read_history()[0].session_body == BODY
        retry_prompt = build_resume_prompt(1, run.session_body, updated_body)
        assert "Implement Korean locale support." in retry_prompt

        class Delivered:
            def run(self, **kwargs):
                calls.append(kwargs)
                return ExecResult(0, False, "done", kwargs["log_path"], session_id="worker")

        _available_executor(
            1, c.ctx, Delivered(), session_body=updated_body, resume_prompt=retry_prompt,
            worktree=c.ctx.repo_root, prompt="full prompt", model=None, log_path=c.store.log_path(1),
        )
        assert calls[-1]["resume_session_id"] == "worker"
        assert "Implement Korean locale support." in calls[-1]["prompt"]
        assert reloaded.get_run(1).session_body == updated_body
    finally:
        c.store.finish_run(1, "failed")
        c.tracker.remove_labels(1, ["running"])


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_interrupted_worker_keeps_lifecycle_identity_in_state_and_history(run_case, monkeypatch, executor):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor=executor)
    c.ctx.executor_factory = executor_for

    def interrupted(argv, **kwargs):
        event = ({"type": "thread.started", "thread_id": "interrupted-worker"} if executor == "codex" else
                 {"type": "system", "subtype": "init", "session_id": "interrupted-worker"})
        output = json.dumps(event) + "\n"
        kwargs["stdout_path"].write_text(output)
        with kwargs["log_path"].open("a") as stream:
            stream.write(output)
        raise SystemExit(143)

    monkeypatch.setattr("foragent.executors.run_logged", interrupted)
    try:
        with pytest.raises(SystemExit) as stopped:
            run_issue(1, c.ctx)
        assert stopped.value.code == 143
        reloaded = StateStore(c.store.root)
        run = reloaded.get_run(1)
        assert run.status == "finished" and run.outcome == "killed"
        for recorded in (run, reloaded.read_history()[0]):
            assert recorded.session_id == "interrupted-worker"
            assert recorded.session_executor == executor
            assert recorded.session_ids == ["interrupted-worker"]
            assert recorded.session_body == BODY
        c.now[0] += timedelta(seconds=1)
        reloaded = StateStore(c.store.root, now=c.ctx.now)
        restarted = reloaded.start_run(1, branch="branch", worktree=c.store.worktree_path(1))
        assert restarted.session_id == "interrupted-worker"
    finally:
        if c.store.get_run(1).status != "finished":
            c.store.finish_run(1, "killed")
        c.tracker.remove_labels(1, ["running"])

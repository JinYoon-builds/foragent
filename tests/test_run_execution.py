from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
import sys

import pytest

from foragent.executors import ExecResult
from foragent.reasons import Reason
from foragent.runner import run_issue
from runner_support import COMMAND, enable_reviewer, run_case, scripted_results


def test_executor_nonzero_exit_still_relies_on_independent_checks(run_case):
    c = run_case
    c.executor.exit_code = 1
    assert run_issue(1, c.ctx) == "merged"


def test_log_growth_poll_updates_progress_and_quiet_poll_does_not(run_case):
    c = run_case
    observed = []

    def poll_hook(execute, poll):
        result = execute()
        before = c.store.get_run(1).last_progress_at
        activity_before = c.store.get_run(1).last_activity_at
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == before
        assert c.store.get_run(1).last_activity_at == activity_before
        c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
        with c.store.log_path(1).open("a") as stream:
            stream.write("new executor output\n")
        c.now[0] += timedelta(seconds=30)
        poll()
        observed.append(c.store.get_run(1).last_progress_at)
        assert observed[-1] == c.now[0].isoformat()
        assert c.store.get_run(1).last_activity_at == observed[-1]
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == observed[-1]
        assert c.store.get_run(1).last_activity_at == observed[-1]
        return result

    c.ctx.poll_hook = poll_hook
    assert run_issue(1, c.ctx) == "merged"
    assert len(observed) == 1


def test_short_executor_records_activity_without_a_periodic_poll(run_case, monkeypatch):
    c = run_case
    execute = c.executor.run

    def short_run(**kwargs):
        result = execute(**kwargs)
        c.now[0] += timedelta(seconds=7)
        kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
        with kwargs["log_path"].open("a") as stream:
            stream.write("completed before the first poll\n")
        return result

    monkeypatch.setattr(c.executor, "run", short_run)
    c.ctx.poll_hook = lambda execute, poll: execute()
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).last_activity_at == c.now[0].isoformat()


@pytest.mark.parametrize("acceptance_dir", ["tests/acceptance/", "checks with spaces", "./checks/"])
def test_claude_native_goal_settings_and_independent_retry(run_case, acceptance_dir):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude", acceptance_dir=acceptance_dir)
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    settings_path = c.store.root / "runs/issue-1/claude-settings.json"
    settings = json.loads(settings_path.read_text())
    expected_command = shlex.join([
        sys.executable, "-m", "foragent.cli", "gate", "check-done",
        "--snapshot", str(c.store.root / "approvals/1.json"),
        "--worktree", str(c.store.worktree_path(1)), "--acceptance-dir", acceptance_dir,
    ])
    path = Path(acceptance_dir).as_posix().rstrip("/") + "/"
    assert settings == {
        "permissions": {"deny": [f"Edit({path}**)", f"Write({path}**)",
                                 "Bash(git commit:*)", "Bash(git push:*)",
                                 "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git merge:*)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": expected_command,
                                      "timeout": c.ctx.config.max_runtime_minutes * 60}]}]},
    }
    assert not settings_path.is_relative_to(c.store.worktree_path(1))
    assert (settings_path.parent / "stop-block-count").read_text() == "0"
    expected = ('Every command in the "Done when" list below exits 0 when run from the repository root, '
                f'nothing under {acceptance_dir} is modified, and no files outside this repository are changed. '
                'Or stop after 40 turns.')
    assert len(c.executor.calls) == 2
    for call in c.executor.calls:
        assert call["goal"] == expected
        assert call["prompt"].startswith("You are implementing GitHub issue #1")
        assert "Do not modify anything under" not in call["prompt"]
        assert COMMAND in call["prompt"] and "Other features." in call["prompt"]
        assert ".foragent/TRY.md" in call["prompt"]
        assert call["settings_path"] == settings_path
    assert "feature not ready" in c.executor.calls[1]["prompt"]
    assert c.store.log_path(1).read_text().count(f"Done when command:\n{COMMAND}") == 2


def test_claude_deny_paths_are_relative_even_for_absolute_acceptance_dir(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude",
                           acceptance_dir=str(c.store.worktree_path(1) / "tests/acceptance"))
    assert run_issue(1, c.ctx) == "merged"
    settings = json.loads((c.store.root / "runs/issue-1/claude-settings.json").read_text())
    assert settings["permissions"]["deny"][:2] == ["Edit(tests/acceptance/**)", "Write(tests/acceptance/**)"]


@pytest.mark.parametrize("event", [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": []}},
    {"type": "result", "result": ""},
])
def test_any_claude_stream_line_updates_progress_through_log_poll(run_case, event):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude")
    observed = []

    def poll_hook(execute, poll):
        log = c.store.log_path(1)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        c.now[0] += timedelta(seconds=30)
        poll()
        observed.append(c.store.get_run(1).last_progress_at)
        assert observed[-1] == c.now[0].isoformat()
        return execute()

    c.ctx.poll_hook = poll_hook
    assert run_issue(1, c.ctx) == "merged"
    assert len(observed) == 1


@pytest.mark.parametrize("log_only", [False, True])
def test_transients_preserve_attempt_and_budget_and_fallback_persists(run_case, monkeypatch, log_only):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1, executor_model="primary",
                           executor_fallback_model="fallback")
    c.executor.steps = [{}, {}, {"feature.txt": "wrong"}, {"feature.txt": "done"}]
    scripted_results(c, monkeypatch, [("at capacity", "transient"), ("overloaded", "transient"),
                                     ("done", "normal"), ("done", "normal")], log_only=log_only)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120, 240]
    assert [call["model"] for call in c.executor.calls] == ["primary", "primary", "fallback", "fallback"]
    assert [call["timeout_s"] for call in c.executor.calls] == [60] * 4
    assert c.executor.calls[0]["prompt"] == c.executor.calls[1]["prompt"] == c.executor.calls[2]["prompt"]
    run = c.store.get_run(1)
    assert run.attempts == 2 and run.waited_seconds == 360
    assert run.fallback_model == "fallback" and run.waiting_until is None
    assert "Executor fallback model: fallback" in c.tracker.created_prs[0].body


def test_transient_budget_exhaustion_is_not_an_implementation_failure(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{}] * 5
    scripted_results(c, monkeypatch, [("503", "transient")] * 5)
    waits = []
    c.ctx.sleep = lambda seconds: waits.append(seconds) or c.now.__setitem__(0, c.now[0] + timedelta(seconds=seconds))
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert waits == [120, 240, 480, 960]
    assert c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).waited_seconds == 1800
    assert c.store.get_run(1).executor_waited_seconds == 1800
    assert "Executor unavailable (transient errors)" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []


def test_previous_log_errors_do_not_classify_the_current_execution(run_case):
    c = run_case
    c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
    c.store.log_path(1).write_text("503 overloaded usage limit quota\n")
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).waited_seconds == 0


def test_implementer_limit_pauses_lane_and_returns_ready_without_attempt(run_case, monkeypatch):
    c = run_case
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {}}, limit_wait_minutes=15)
    c.executor.steps = [{}]
    scripted_results(c, monkeypatch, [("429 usage limit", "limit")])
    assert run_issue(1, c.ctx) == "limit-wait"
    run = c.store.get_run(1)
    assert run.outcome == "limit-wait" and run.attempts == 1 and run.waited_seconds == 0
    assert c.store.lane_paused("ui", c.now[0] + timedelta(minutes=14))
    assert not c.store.lane_paused("ui", c.now[0] + timedelta(minutes=15))
    assert not c.store.lane_paused("main", c.now[0])
    assert c.tracker.get_issue(1).labels == {"approved", "ready"}
    assert c.store.load_flag(1) is None and c.tracker.created_prs == []


@pytest.mark.parametrize("message", ["overloaded", "429 quota"])
def test_reviewer_availability_waits_then_uses_recovered_verdict(run_case, message):
    c = run_case
    calls = enable_reviewer(c, [ExecResult(1, False, message, Path("unused"),
                                        error_kind="limit" if "429" in message else "transient"), "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 2 and c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).waited_seconds == 120
    assert not c.store.lane_paused("main", c.now[0])
    assert "Round 1: VERDICT: PASS" in c.tracker.created_prs[0].body
    assert "Round 2" not in c.tracker.created_prs[0].body


def test_reviewer_limit_only_becomes_unusable_after_wait_budget(run_case):
    c = run_case
    c.executor.steps.append({})
    c.ctx.config = replace(c.ctx.config, transient_max_wait_minutes=2)
    limit = ExecResult(1, False, "usage limit", Path("unused"), error_kind="limit")
    calls = enable_reviewer(c, [limit, limit, limit])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 3 and c.store.get_run(1).waited_seconds == 120
    assert c.store.get_run(1).attempts == 2
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert not c.store.lane_paused("main", c.now[0])


def test_reviewer_transient_exhaustion_uses_executor_unavailable_flag(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, transient_max_wait_minutes=2)
    transient = ExecResult(1, False, "overloaded", Path("unused"), error_kind="transient")
    calls = enable_reviewer(c, [transient, transient])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 2 and c.store.get_run(1).waited_seconds == 120
    assert c.store.get_run(1).attempts == 1
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Executor unavailable (transient errors)" in c.tracker.list_comments(1)[-1].body


def test_transient_timeout_result_retries_when_runtime_budget_remains(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{}, {"feature.txt": "done"}]
    execute = c.executor.run

    def run(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 1:
            return replace(result, exit_code=-9, timed_out=True, last_message="stream disconnected", error_kind="transient")
        return result

    monkeypatch.setattr(c.executor, "run", run)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert c.store.get_run(1).attempts == 1 and c.store.get_run(1).waited_seconds == 120


@pytest.mark.parametrize("message,success", [
    ("Tests failed: expected quota=10, got quota=0", False),
    ("Implemented quota accounting", True),
    ("HTTP 503 test case for overloaded services", False),
])
def test_implementation_text_does_not_pause_or_backoff(run_case, monkeypatch, message, success):
    c = run_case
    c.executor.exit_code = 0 if success else 1
    c.executor.steps = [{"feature.txt": "done" if success else "wrong"}] * 2
    scripted_results(c, monkeypatch, [(message, "normal")] * 2, log_only=True)
    assert run_issue(1, c.ctx) == ("merged" if success else "retries-exceeded")
    run = c.store.get_run(1)
    assert run.waited_seconds == 0 and run.attempts == (1 if success else 2)
    assert not c.store.lane_paused("main", c.now[0])

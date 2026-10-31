from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
import sys

import pytest

from foragent.executors import ExecResult
from foragent.reasons import Reason
from foragent.run_prompts import goal_condition
from foragent.runner import run_issue
from runner_support import COMMAND, enable_reviewer, run_case, scripted_results


@pytest.mark.parametrize("failure", ["exit-error", "launch-error"])
def test_failed_resume_is_counted_once_even_when_fresh_launch_also_fails(run_case, failure):
    from foragent.run_execution import _available_executor
    from foragent.state import StateStore

    c = run_case
    c.store.update_run(1, session_id="saved", session_executor=c.ctx.config.executor)
    calls = []

    class Executor:
        def run(self, **kwargs):
            calls.append(kwargs)
            if "resume_session_id" in kwargs:
                if failure == "launch-error":
                    raise OSError("resume executable unavailable")
                kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
                kwargs["log_path"].write_text("CLI stderr: saved transcript unavailable\n")
                return ExecResult(1, False, "resume refused", kwargs["log_path"])
            raise OSError("fresh executable unavailable")

    with pytest.raises(OSError, match="fresh executable unavailable"):
        _available_executor(
            1, c.ctx, Executor(), session_body="body", resume_prompt="continue",
            worktree=c.ctx.repo_root, prompt="full task", model=None, log_path=c.store.log_path(1),
        )
    assert len(calls) == 2
    assert calls[0]["prompt"] == "continue" and calls[1]["prompt"] == "full task"
    assert StateStore(c.store.root).get_run(1).resume_failures == 1
    log = c.store.log_path(1).read_text()
    if failure == "launch-error":
        assert "resume executable unavailable" in log
    else:
        assert "resume refused" in log and "CLI stderr: saved transcript unavailable" in log
    c.store.finish_run(1, "executor-error")
    c.tracker.remove_labels(1, ["running"])


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
    expected = goal_condition(acceptance_dir)
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


def test_assigned_model_survives_transients_instead_of_the_configured_fallback(run_case, monkeypatch):
    """D80: an issue assignment's model is an explicit choice; recovery never swaps in the fallback."""
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1, executor_model="lane-model",
                           executor_fallback_model="fallback")
    c.store.update_run(1, executor=c.ctx.config.executor, executor_model="assigned")
    c.executor.steps = [{}, {}, {"feature.txt": "wrong"}, {"feature.txt": "done"}]
    scripted_results(c, monkeypatch, [("at capacity", "transient"), ("overloaded", "transient"),
                                     ("done", "normal"), ("done", "normal")])

    def sleep(seconds):
        c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    assert run_issue(1, c.ctx) == "merged"
    assert [call["model"] for call in c.executor.calls] == ["assigned"] * 4
    run = c.store.get_run(1)
    assert run.fallback_model is None and run.executor_model == "assigned"
    assert "Executor fallback model" not in c.tracker.created_prs[0].body


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


def test_reviewer_availability_waits_then_uses_recovered_verdict(run_case):
    c = run_case
    calls = enable_reviewer(c, [ExecResult(1, False, "overloaded", Path("unused"), error_kind="transient"),
                                "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 2 and c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).waited_seconds == 120
    assert not c.store.lane_paused("main", c.now[0])
    assert "Round 1: VERDICT: PASS" in c.tracker.created_prs[0].body
    assert "Round 2" not in c.tracker.created_prs[0].body


def test_reviewer_unreadable_limit_pauses_its_executor_for_configured_wait(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, limit_wait_minutes=15)
    limit = ExecResult(1, False, "usage limit", Path("unused"), error_kind="limit")
    calls = enable_reviewer(c, [limit, limit])
    assert run_issue(1, c.ctx) == "limit-wait"
    assert len(calls) == 1 and c.store.get_run(1).waited_seconds == 0
    assert c.store.executor_paused("codex", c.now[0] + timedelta(minutes=14))
    assert not c.store.executor_paused("codex", c.now[0] + timedelta(minutes=15))
    assert not c.store.executor_paused("claude", c.now[0])
    assert c.store.load_flag(1) is None


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


def _codex_usage(reset, percent=100):
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": percent, "window_minutes": 300, "resets_at": int(reset.timestamp())}}}})


def _claude_usage(reset):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": int(reset.timestamp()), "rateLimitType": "five_hour"}})


def test_implementer_limit_pauses_its_executor_until_reported_reset(run_case, monkeypatch):
    c = run_case
    reset = (c.now[0] + timedelta(hours=2)).replace(microsecond=0)
    event = _claude_usage(reset) if c.ctx.config.executor == "claude" else _codex_usage(reset)
    scripted_results(c, monkeypatch, [(event, "limit")], log_only=True)
    assert run_issue(1, c.ctx) == "limit-wait"
    assert c.store.executor_paused(c.ctx.config.executor, reset - timedelta(seconds=1))
    assert not c.store.executor_paused(c.ctx.config.executor, reset)
    assert c.store.lane_paused("main", reset - timedelta(seconds=1))


def test_reviewer_limit_with_exhausted_usage_waits_for_reset_without_retrying(run_case):
    c = run_case
    reset = (c.now[0] + timedelta(hours=3)).replace(microsecond=0)
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
        with kwargs["log_path"].open("a") as stream:
            stream.write(_codex_usage(reset) + "\n")
        return ExecResult(1, False, "usage limit", kwargs["log_path"], error_kind="limit")

    enable_reviewer(c, [])
    reviewer = c.ctx.executor_factory("codex")
    reviewer.run = review
    assert run_issue(1, c.ctx) == "limit-wait"
    assert len(calls) == 1 and c.store.get_run(1).waited_seconds == 0
    assert c.store.executor_paused("codex", reset - timedelta(seconds=1))
    assert not c.store.executor_paused("codex", reset)
    assert not c.store.executor_paused("claude", c.now[0])
    assert c.store.load_flag(1) is None
    assert "ready" in c.tracker.get_issue(1).labels and "running" not in c.tracker.get_issue(1).labels


def test_running_issue_does_not_launch_an_executor_paused_by_another_run(run_case):
    c = run_case
    reset = c.now[0] + timedelta(hours=1)
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    c.store.pause_executor("codex", reset)  # e.g. another lane's reviewer hit the codex limit
    assert run_issue(1, c.ctx) == "limit-wait"
    assert calls == [] and c.store.get_run(1).waited_seconds == 0
    assert c.store.lane_paused("main", reset - timedelta(seconds=1))
    assert not c.store.lane_paused("main", reset)
    assert "ready" in c.tracker.get_issue(1).labels and c.store.load_flag(1) is None
    assert c.tracker.created_prs == []


def test_paused_implementer_is_not_launched_until_its_reset(run_case):
    c = run_case
    reset = c.now[0] + timedelta(minutes=30)
    c.store.pause_executor(c.ctx.config.executor, reset)
    before = list(c.executor.steps)
    assert run_issue(1, c.ctx) == "limit-wait"
    assert c.executor.steps == before
    assert c.store.executor_pause_until(c.ctx.config.executor, c.now[0]) == reset


def test_undelivered_feedback_survives_an_executor_error(run_case):
    """#420: an executor error before the turn ran keeps the pending check failures and their whole flag."""
    from foragent.run_execution import _available_executor

    c = run_case
    pending = "Command: a\nfirst failure\n\nCommand: b\nsecond failure"
    c.store.update_run(1, resume_reason=pending, resume_whole=True)

    class Executor:
        def run(self, **kwargs):
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["log_path"].write_text("executor crashed\n")
            return ExecResult(1, False, "executor crashed", kwargs["log_path"])

    _available_executor(1, c.ctx, Executor(), session_body="body", resume_prompt=None,
                        worktree=c.ctx.repo_root, prompt="full task", model=None, log_path=c.store.log_path(1))
    run = c.store.get_run(1)
    assert run.resume_reason.startswith(pending + "\n\nExecutor exit_code=1") and run.resume_whole
    c.store.finish_run(1, "executor-error")
    c.tracker.remove_labels(1, ["running"])


def test_reviewer_limited_during_review_preparation_moves_to_the_worker_once(run_case):
    """#186: the stored reviewer hits its limit after the review began preparing; the launch moves it to
    the run's own executor instead of limit-wait, and a later round adds no second same-model event."""
    import json as _json

    from foragent.config import resolved_reviewer
    from foragent.quota_reviewer import moving_review
    from foragent.run_execution import _available_executor

    c = run_case
    codex_calls = enable_reviewer(c, [])
    c.executor.steps = [{}, {}]
    c.store.update_run(1, executor="claude", reviewer="codex")  # The pair the tick stored.
    reviewers = []

    @moving_review
    def review(n, ctx):
        reviewers.append(resolved_reviewer(ctx.config))
        c.store.pause_executor("codex", c.now[0] + timedelta(hours=1))  # The limit hits while preparing.
        return _available_executor(n, ctx, ctx.executor_factory(reviewers[-1]), reviewer=True, prompt="review",
                                   worktree=c.history.clone, log_path=c.store.log_path(1), timeout_s=60)

    for _ in range(2):
        assert review(1, c.ctx).exit_code == 0
    assert reviewers == ["codex", "claude", "claude"] and codex_calls == [] and len(c.executor.calls) == 2
    assert "running" in c.tracker.get_issue(1).labels, "never a limit-wait"
    rows = [_json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    moves = [row for row in rows if row["kind"] == "same-model-review"]
    assert [(row["reason"], row["run"]) for row in moves] == [("limit", c.store.get_run(1).started_at)]
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


def test_reviewer_limit_written_between_launch_questions_cannot_split_their_answers(run_case, monkeypatch):
    """#186: the move check and the launch hold read one quota snapshot. A Codex limit recorded just
    after the move check said "free" must not turn the same launch into a limit-wait."""
    from foragent import quota_reviewer
    from foragent.run_execution import _available_executor

    c = run_case
    codex_calls = enable_reviewer(c, ["VERDICT: PASS"])
    c.store.update_run(1, executor="claude", reviewer="codex")  # The pair the tick stored.
    check = quota_reviewer.reviewer_limited

    def then_limited(n, ctx, name, view):
        answer = check(n, ctx, name, view)
        c.store.record_usage("codex", [{"name": "5h", "used_percent": 100,
                                        "resets_at": (c.now[0] + timedelta(hours=2)).isoformat()}])
        return answer

    monkeypatch.setattr(quota_reviewer, "reviewer_limited", then_limited)
    result = _available_executor(1, c.ctx, c.ctx.executor_factory("codex"), reviewer=True, prompt="review",
                                 worktree=c.history.clone, log_path=c.store.log_path(1), timeout_s=60)
    assert result.last_message == "VERDICT: PASS" and len(codex_calls) == 1
    assert "running" in c.tracker.get_issue(1).labels and "ready" not in c.tracker.get_issue(1).labels
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


def test_late_reviewer_limit_with_a_worker_that_cannot_review_waits_instead_of_crashing(run_case):
    """#186: with an unlimited worker, a reviewer limited late is not removed (no executor ``none``);
    the launch is an ordinary limit-wait that hands the issue back to the queue."""
    from foragent.executors import executor_for
    from foragent.quota_reviewer import moving_review
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_execution import _available_executor

    c = run_case
    enable_reviewer(c, [])
    c.ctx.config = replace(c.ctx.config, executor="local", reviewer="claude",
                           executor_list=[{"name": "local", "quota": "unlimited"},
                                          {"name": "claude", "quota": "subscription"}])
    asked = []
    c.ctx.executor_factory = lambda name: asked.append(name) or executor_for(name) if name == "none" else c.executor
    c.store.update_run(1, executor="local", reviewer="claude")

    @moving_review
    def review(n, ctx):
        c.store.pause_executor("claude", c.now[0] + timedelta(hours=1))  # The limit hits while preparing.
        reviewer = ctx.config.reviewer
        return _available_executor(n, ctx, ctx.executor_factory(reviewer), reviewer=True, prompt="review",
                                   worktree=c.history.clone, log_path=c.store.log_path(1), timeout_s=60)

    with pytest.raises(ExecutorStopped) as stopped:
        review(1, c.ctx)
    assert stopped.value.outcome == "limit-wait" and asked == [] and c.executor.calls == []
    assert "ready" in c.tracker.get_issue(1).labels and "running" not in c.tracker.get_issue(1).labels
    c.store.finish_run(1, "limit-wait")


def test_rerun_after_a_launch_move_decision_does_not_ask_again(run_case, monkeypatch):
    """#186: once a launch decided the reviewer moves, the rerun uses the worker even if the limit
    reads as cleared right after; it neither asks again nor raises a second ReviewerLimited."""
    from foragent import quota_reviewer
    from foragent.quota_reviewer import moving_review
    from foragent.run_execution import _available_executor

    c = run_case
    codex_calls = enable_reviewer(c, [])
    c.executor.steps = [{}]
    c.store.update_run(1, executor="claude", reviewer="codex")
    answers = iter([True])
    monkeypatch.setattr(quota_reviewer, "reviewer_limited", lambda n, ctx, name, view: next(answers, False))
    reviewers = []

    @moving_review
    def review(n, ctx):
        reviewers.append(ctx.config.reviewer)
        return _available_executor(n, ctx, ctx.executor_factory(ctx.config.reviewer), reviewer=True,
                                   prompt="review", worktree=c.history.clone, log_path=c.store.log_path(1),
                                   timeout_s=60)

    assert review(1, c.ctx).exit_code == 0
    assert reviewers == ["codex", "claude"] and codex_calls == [] and len(c.executor.calls) == 1
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])

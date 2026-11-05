"""The run keeps what its CI concluded as one fact in its own record (``ci_result``, #1821).

The view reads that fact as is, so no later history (tick records, rotation) changes it. These
tests drive the real runner (the locked ci-mode and ci-not-started scenarios) and the CI wait.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_ci_mode import (  # noqa: E402,F401
    ci_evidence_recording, isolated_home, mode_case, no_external_execution, offline_acceptance, run,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def result(case, number=1):
    found = case.store.get_run(number)
    return case.store.ci_result(number, found.started_at)


def test_a_passing_run_records_passed_from_its_ci_wait(git_history, tmp_path, monkeypatch):
    from foragent import watch_issue

    case = mode_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "merged"
    assert result(case) == "passed"
    assert watch_issue.ci_state(case.store, 1) == "passed"


def test_a_comparison_failure_after_githubs_pass_records_failed(git_history, tmp_path, monkeypatch):
    from foragent import watch_issue

    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.regression = "nonpassing"
    case.implementer.steps.append(lambda kwargs: "No real fix")
    case.reviewer.steps += ["VERDICT: PASS"] * 4
    assert run(case) == "retries-exceeded"
    assert result(case) == "failed"
    assert watch_issue.ci_state(case.store, 1) == "failed"


def test_jobs_that_never_started_record_not_started(git_history, tmp_path, monkeypatch):
    from foragent import watch_issue
    from foragent.runner import run_issue
    from test_ci_not_started import ci_case, job, no_signals  # noqa: F401

    case, _ = ci_case(git_history, tmp_path, monkeypatch, [job()], max_retries=1)
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "held"
    assert result(case) == "not_started"
    assert watch_issue.ci_state(case.store, 1) == "not_started"


def _wait_case(tmp_path, monkeypatch, *, remaining=600, logs=None):
    from foragent import run_publish, runner
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    tracker = SimpleNamespace(pr_head_sha=lambda pr: "abc", pr_failed_logs=lambda pr, **kwargs: logs)
    ctx = SimpleNamespace(store=store, tracker=tracker, repo="o/r", now=lambda: NOW,
                          config=SimpleNamespace(checks_fallback=None))
    monkeypatch.setattr(runner, "_remaining", lambda n, ctx: remaining)
    monkeypatch.setattr(run_publish, "_progress", lambda n, ctx: None)
    monkeypatch.setattr(run_publish, "flag", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_publish, "_timeout", lambda n, ctx: "timeout")
    return store, ctx, SimpleNamespace(root=tmp_path)


def test_comparison_mode_jobs_that_never_started_record_not_started(tmp_path, monkeypatch):
    from foragent import run_publish
    from foragent.tracker import CIEvidence

    store, ctx, git = _wait_case(tmp_path, monkeypatch, logs=CIEvidence("Job did not start", not_started=True))
    wait = run_publish._CIWait(NOW.timestamp())
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, "fail",
                                    comparison_mode=True) is None
    assert wait.infrastructure
    assert store.ci_result(1, store.get_run(1).started_at) == "not_started"


def test_a_ci_wait_past_its_deadline_records_timed_out(tmp_path, monkeypatch):
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    wait = run_publish._CIWait(NOW.timestamp() - 4 * 60 * 60)  # well past the 20-minute wait
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, "pending") == "timeout"
    assert store.ci_result(1, store.get_run(1).started_at) == "timed_out"


def test_a_run_out_of_time_during_the_wait_records_timed_out(tmp_path, monkeypatch):
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch, remaining=0)
    wait = run_publish._CIWait(NOW.timestamp())
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, "pending") == "timeout"
    assert store.ci_result(1, store.get_run(1).started_at) == "timed_out"


import pytest  # noqa: E402


@pytest.mark.parametrize("status, recorded", [("pass", "passed"), ("none", "no_checks")])
@pytest.mark.parametrize("observed", [False, True], ids=["read-in-the-wait", "observed-under-the-lock"])
def test_a_wait_that_ends_on_a_status_records_it(tmp_path, monkeypatch, status, recorded, observed):
    """A status the caller observed under the merge lock ends the wait without a fresh read."""
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    ctx.tracker.pr_checks = lambda pr: status
    wait = run_publish._CIWait(NOW.timestamp())
    given = status if observed else None
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, given) is None
    assert store.ci_result(1, store.get_run(1).started_at) == recorded


def test_comparison_mode_wait_past_its_deadline_with_local_fallback_records_timed_out(tmp_path, monkeypatch):
    """Review 7: checks_fallback local ends a pending wait as infrastructure; CI did not finish."""
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    ctx.config.checks_fallback = "local"
    wait = run_publish._CIWait(NOW.timestamp() - 4 * 60 * 60)
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, "pending",
                                    comparison_mode=True) is None
    assert wait.infrastructure
    assert store.ci_result(1, store.get_run(1).started_at) == "timed_out"


def test_reads_and_holds_outside_the_runs_ci_wait_record_nothing(tmp_path, monkeypatch):
    """The final merge read and the merge owner's holds, failures and comparisons are not the run's wait."""
    from foragent import run_publish
    from foragent.tracker import CIEvidence

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    started = store.get_run(1).started_at
    ctx.tracker.pr_checks = lambda pr: "fail"
    monkeypatch.setattr(run_publish, "_append", lambda *args, **kwargs: None)
    assert run_publish._ci_status(ctx, 2, tmp_path, NOW.timestamp()) == "fail"
    run_publish._record_failure(1, ctx, 2, "abc")
    with pytest.raises(run_publish.ExecutorStopped):
        run_publish._hold_if_not_started(1, ctx, 2, CIEvidence("Job did not start", not_started=True))
    assert store.ci_result(1, started) is None


def test_a_wait_bound_to_an_older_run_never_writes_into_a_newer_one(tmp_path, monkeypatch):
    """Review 7: a direct rerun starts while an older execution's CI conclusion is being recorded."""
    from datetime import timedelta
    from foragent import run_publish, run_publish_ci

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    clock = [NOW]
    store._now = lambda: clock[0]
    with run_publish_ci._run_ci_wait(1, ctx):
        clock[0] += timedelta(minutes=1)
        store.finish_run(1, "merge-queued")
        clock[0] += timedelta(minutes=1)
        newer = store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
        run_publish._record_result(1, ctx, "passed")
    assert store.ci_result(1, newer.started_at) is None


class RateLimited(Exception):
    """Stands in for gh's HTTP 429 / limit-wait stop anywhere after the status read."""


@pytest.mark.parametrize("comparison_mode", [False, True], ids=["checks", "comparison"])
def test_a_failing_status_is_recorded_before_any_rate_limited_log_read(tmp_path, monkeypatch, comparison_mode):
    """Review 8: checks fail, then reading the failure logs (wait or callback) hits HTTP 429."""
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch)

    def limited(*args, **kwargs):
        raise RateLimited

    ctx.tracker.pr_failed_logs = limited
    wait = run_publish._CIWait(NOW.timestamp())
    with pytest.raises(RateLimited):
        run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, limited, "fail",
                                 comparison_mode=comparison_mode)
    assert store.ci_result(1, store.get_run(1).started_at) == "failed"


def test_a_ci_deadline_is_recorded_before_a_rate_limited_flag(tmp_path, monkeypatch):
    from foragent import run_publish

    store, ctx, git = _wait_case(tmp_path, monkeypatch)

    def limited(*args, **kwargs):
        raise RateLimited

    monkeypatch.setattr(run_publish, "flag", limited)
    wait = run_publish._CIWait(NOW.timestamp() - 4 * 60 * 60)
    with pytest.raises(RateLimited):
        run_publish._wait_for_ci(1, ctx, git, 2, "abc", NOW.timestamp(), wait, lambda: None, "pending")
    assert store.ci_result(1, store.get_run(1).started_at) == "timed_out"


def test_a_failure_outside_the_runs_wait_belongs_to_the_admitted_execution(tmp_path, monkeypatch):
    """Review 8: an owner comparison for an older admission reports a regression while a newer run lives."""
    from datetime import timedelta
    from foragent import merge_owner, run_publish, run_publish_ci, watch_issue

    store, ctx, git = _wait_case(tmp_path, monkeypatch)
    clock = [NOW]
    store._now = lambda: clock[0]
    older = store.get_run(1).started_at
    with run_publish_ci._run_ci_wait(1, ctx):
        assert run_publish._failing_execution(1, ctx) == older, "inside the run's wait: that run"
    clock[0] += timedelta(minutes=1)
    store.finish_run(1, "merge-queued")
    clock[0] += timedelta(minutes=1)
    newer = store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {(1, older): {}, (2, "other"): {}})
    run_publish._record_failure(1, ctx, 2, "abc")
    tags = [f["run_started_at"] for f in store.flow_records() if f["kind"] == "ci-failure"]
    assert tags == [older], "the owner's failure names the admitted execution, never the live rerun"
    clock[0] += timedelta(minutes=1)
    store.update_run(1, phase="implementing", phase_started_at=clock[0].isoformat())
    store.finish_run(1, "crashed")
    assert newer.started_at != older
    assert watch_issue.ci_state(store, 1) == "unrecorded", "the newer run never reached CI"
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {})
    run_publish._record_failure(1, ctx, 2, "abc")
    assert [f["run_started_at"] for f in store.flow_records() if f["kind"] == "ci-failure"][-1] is None

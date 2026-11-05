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


def test_a_passing_run_records_passed_from_the_wait_and_the_final_merge_read(git_history, tmp_path, monkeypatch):
    from foragent import run_publish, watch_issue

    calls = []
    original = run_publish._ci_status

    def status(*args, **kwargs):
        import traceback

        caller = next(frame.name for frame in reversed(traceback.extract_stack()[:-1])
                      if frame.name in {"_wait_for_ci", "_final_merge_inputs"})
        calls.append((caller, kwargs.get("n")))
        return original(*args, **kwargs)

    monkeypatch.setattr(run_publish, "_ci_status", status)
    case = mode_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "merged"
    assert {caller for caller, _ in calls} == {"_wait_for_ci", "_final_merge_inputs"}, calls
    assert all(n == 1 for _, n in calls), "every CI read the run acts on is the run's own"
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


@pytest.mark.parametrize("status, recorded", [("pass", "passed"), ("none", "no_checks"), ("fail", "failed"),
                                              ("pending", None)])
def test_each_ci_status_the_run_reads_is_its_result(tmp_path, monkeypatch, status, recorded):
    from foragent import run_publish

    store, ctx, _ = _wait_case(tmp_path, monkeypatch)
    ctx.tracker.pr_checks = lambda pr: status
    started = store.get_run(1).started_at
    assert run_publish._ci_status(ctx, 2, tmp_path, NOW.timestamp(), n=1) == status
    assert store.ci_result(1, started) == recorded
    assert run_publish._ci_status(ctx, 2, tmp_path, NOW.timestamp()) == status
    assert store.ci_result(1, started) == recorded, "a read for no run records nothing"

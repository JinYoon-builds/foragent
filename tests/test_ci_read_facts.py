"""The runner keeps every CI read it acts on for a run as a local ``ci-read`` fact.

``ci-failure`` (#1727) keeps code failures; ``ci-read`` keeps what the CI wait and the final merge
inputs read (and jobs that never started), so a finished run's CI result is read from facts instead
of guessed from the step it stopped in (#1821). The locked ci-mode scenario is reused, so CI and
its comparison are the real ones.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_ci_mode import (  # noqa: E402,F401
    ci_evidence_recording, isolated_home, mode_case, no_external_execution, offline_acceptance, run,
)


def reads(case):
    return [f["data"]["status"] for f in case.store.flow_records() if f["kind"] == "ci-read"]


def test_a_passing_run_keeps_the_wait_read_and_the_final_merge_read(git_history, tmp_path, monkeypatch):
    from foragent import run_publish, watch_issue

    callers = []
    original = run_publish._ci_status

    def status(*args, **kwargs):
        import traceback

        callers.append(next(frame.name for frame in reversed(traceback.extract_stack()[:-1])
                            if frame.name in {"_wait_for_ci", "_final_merge_inputs"}))
        return original(*args, **kwargs)

    monkeypatch.setattr(run_publish, "_ci_status", status)
    case = mode_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "merged"
    assert {"_wait_for_ci", "_final_merge_inputs"} <= set(callers), callers
    assert len(reads(case)) == len(callers), "every CI read the run acts on is kept, once"
    assert reads(case)[-1] == "pass"
    assert watch_issue.ci_state(case.store, 1) == "passed"


def test_a_comparison_failure_after_githubs_pass_reads_as_failed(git_history, tmp_path, monkeypatch):
    from foragent import watch_issue

    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.regression = "nonpassing"
    case.implementer.steps.append(lambda kwargs: "No real fix")
    case.reviewer.steps += ["VERDICT: PASS"] * 4
    assert run(case) == "retries-exceeded"
    assert "pass" in reads(case)
    assert any(f["kind"] == "ci-failure" for f in case.store.flow_records())
    assert watch_issue.ci_state(case.store, 1) == "failed"


def test_jobs_that_never_started_read_as_not_started(git_history, tmp_path, monkeypatch):
    """The hold path for CI jobs that never ran (billing, provisioning): no pass, no code failure."""
    from foragent import watch_issue
    from foragent.runner import run_issue
    from test_ci_not_started import ci_case, job, no_signals  # noqa: F401

    case, _ = ci_case(git_history, tmp_path, monkeypatch, [job()], max_retries=1)
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "held"
    assert reads(case)[-1] == "not-started"
    assert watch_issue.ci_state(case.store, 1) == "not_started"


def test_comparison_mode_jobs_that_never_started_read_as_not_started(tmp_path, monkeypatch):
    """checks: ci with jobs GitHub never ran (billing): the wait treats it as infrastructure, not a pass."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import run_publish, runner, watch_issue
    from foragent.state import StateStore
    from foragent.tracker import CIEvidence

    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    tracker = SimpleNamespace(pr_head_sha=lambda pr: "abc",
                              pr_failed_logs=lambda pr, **kwargs: CIEvidence("Job did not start", not_started=True))
    ctx = SimpleNamespace(store=store, tracker=tracker, repo="o/r", now=lambda: now)
    monkeypatch.setattr(runner, "_remaining", lambda n, ctx: 600)
    monkeypatch.setattr(run_publish, "_progress", lambda n, ctx: None)
    wait = run_publish._CIWait(now.timestamp())
    git = SimpleNamespace(root=tmp_path)
    assert run_publish._wait_for_ci(1, ctx, git, 2, "abc", now.timestamp(), wait, lambda: None, "fail",
                                    comparison_mode=True) is None
    assert wait.infrastructure
    assert [f["data"]["status"] for f in store.flow_records() if f["kind"] == "ci-read"] == ["not-started"]
    assert watch_issue.ci_state(store, 1) == "not_started", "a live run in its next step, CI never started"

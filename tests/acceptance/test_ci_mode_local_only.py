"""Locked acceptance for ci-mode-local-only. Run through runner.run_issue.

Reuse the repository's offline Git, executor and GitHub recordings. Git writes
use fast-import/fetch plumbing, never commit/push/reset/checkout/stash. The
process recording executes only the tiny fixture suites, including real pytest
selection and JUnit reports; no baseline/comparison/verification code is mocked.
"""

import pytest

from foragent import runner
from test_ci_mode import (
    FUTURE, KEEP, LOCAL_ACCEPTANCE, OTHER, mode_case,
    no_external_execution,  # noqa: F401 -- offline fixture
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


PORTABLE = {
    KEEP, OTHER, FUTURE,
    "tests/acceptance/test_machine.py::test_portable_neighbor",
}




def assert_no_portable_protected_execution(case):
    unexpected = [
        sorted(call.executed & PORTABLE)
        for call in case.processes.calls if call.executed & PORTABLE
    ]
    assert unexpected == [], (
        "checks: ci started a full local protected run without an explicit "
        f"local fallback: {unexpected}"
    )


def assert_local_only_checked_on_head(case):
    assert any(
        LOCAL_ACCEPTANCE in call.executed
        and call.worker_calls >= 1
        and call.cwd == case.store.worktree_path(1)
        for call in case.processes.calls
    ), "Keep enforcing local_only on the implementation head, after the worker ran"




@pytest.mark.parametrize("target", ["main"], ids=["main-control"])  # Keeps the node id running issues captured.
def test_ci_unusable_evidence_reports_without_full_local_protected_run(
    git_history, tmp_path, monkeypatch, target,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.damage = "incomplete-head"

    outcome = runner.run_issue(1, case.ctx)

    assert_no_portable_protected_execution(case)
    assert_local_only_checked_on_head(case)
    assert outcome != "merged" and case.tracker.merged_prs == [], (
        "Unusable CI evidence cannot authorize a merge"
    )
    assert len(case.implementer.calls) == 1, "Unavailable evidence must not consume repair turns"
    flag = case.store.load_flag(1)
    comments = case.tracker.list_comments(1)
    assert flag is not None and comments, "Report the unavailable comparison to the run's owner"
    explanation = "\n".join([
        case.store.get_run(1).detail or "", *(comment.body for comment in comments),
    ]).lower()
    assert (
        "complete" in explanation and ("false" in explanation or "incomplete" in explanation)
    ), (
        "Report the incomplete head evidence"
    )

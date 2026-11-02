"""Locked acceptance for ci-mode-local-only. Run through runner.run_issue.

Reuse the repository's offline Git, executor and GitHub recordings. Git writes
use fast-import/fetch plumbing, never commit/push/reset/checkout/stash. The
process recording executes only the tiny fixture suites, including real pytest
selection and JUnit reports; no baseline/comparison/verification code is mocked.
"""

import pytest

from conftest import git_command
from foragent import runner
from test_ci_mode import (
    FUTURE, KEEP, LOCAL_ACCEPTANCE, OTHER, mode_case,
    no_external_execution,  # noqa: F401 -- offline fixture
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


PARENT = 20
BATCH = f"foragent/batch-{PARENT}"
PORTABLE = {
    KEEP, OTHER, FUTURE,
    "tests/acceptance/test_machine.py::test_portable_neighbor",
}


def observe_processes(case, monkeypatch):
    """Record the exact Git revision at the existing subprocess boundary."""
    observed = []
    process = runner.run_logged

    def observe(argv, *, cwd, **kwargs):
        sha = git_command(cwd, "rev-parse", "HEAD")
        result = process(argv, cwd=cwd, **kwargs)
        observed.append((sha, case.processes.calls[-1]))
        return result

    monkeypatch.setattr(runner, "run_logged", observe)
    return observed


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


@pytest.mark.parametrize("retry", [False, True], ids=["base-move", "base-move-and-retry"])
def test_ci_base_move_to_batch_recaptures_only_local_only(
    git_history, tmp_path, monkeypatch, retry,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.config, "review_paths", {"src/demo.py": "batch"})
    case.tracker.add_issue(PARENT, "Batch parent")
    case.tracker.set_parent(1, PARENT)
    observed = observe_processes(case, monkeypatch)
    moves = []

    def implement(kwargs):
        case.implement(kwargs)
        if not moves:
            # The first capture is on main. A real, different batch base is
            # adopted by the public runner after the worker changes a path
            # whose configured review mode is batch.
            case.history.update({"batch-base.txt": "new base\n"})
            git_command(case.history.origin, "update-ref", f"refs/heads/{BATCH}", case.history.tip)
            moves.append(case.history.tip)
        broken = kwargs["worktree"] / "broken-machine"
        if retry and len(case.implementer.calls) == 1:
            broken.write_text("retry the local_only check\n")
        else:
            broken.unlink(missing_ok=True)

    case.implementer.steps = [implement] * (2 if retry else 1)
    runner.run_issue(1, case.ctx)

    assert moves and moves[0] != case.base_sha, "The scenario must really adopt another base"
    measurements = [
        (sha, call) for sha, call in observed
        if LOCAL_ACCEPTANCE in call.executed
        and (call.worker_calls == 0 or call.cwd != case.store.worktree_path(1))
        and "--collect-only" not in call.words
    ]
    assert [sha for sha, _ in measurements] == [case.base_sha, moves[0]], (
        "Measure local_only on both exact bases, once each; do not skip re-capture"
    )
    assert all(call.executed == {LOCAL_ACCEPTANCE} for _, call in measurements), (
        "The moved-base protected selection expanded beyond local_only"
    )
    assert_no_portable_protected_execution(case)
    assert len(case.implementer.calls) == (2 if retry else 1), "Exercise the requested retry"
    assert_local_only_checked_on_head(case)


@pytest.mark.parametrize("target", ["main", BATCH], ids=["main-control", "initial-batch"])
def test_ci_unusable_evidence_reports_without_full_local_protected_run(
    git_history, tmp_path, monkeypatch, target,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    if target == BATCH:
        object.__setattr__(case.config, "review", "batch")
        case.tracker.add_issue(PARENT, "Batch parent")
        case.tracker.set_parent(1, PARENT)
        git_command(case.history.origin, "update-ref", f"refs/heads/{BATCH}", case.base_sha)
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
    ) or (target.lower() in explanation and "ci" in explanation), (
        "Report the incomplete head evidence, or identify the target that cannot obtain CI evidence"
    )

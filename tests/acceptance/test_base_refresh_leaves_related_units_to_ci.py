"""Acceptance test for base-refresh-leaves-related-units-to-ci. Locked by Foragent; do not edit.

In CI mode, a base refresh after a review PASS re-verifies because main moved
("base moved: result-affecting paths; re-verifying"). That re-verify still runs
Done when, the protected acceptance check and the local_only unit tests, but it
leaves the related own unit test files to PR CI on the new head and records one
plain line saying so. Merge still needs CI evidence for the exact pushed head.
The first check, every check after a review FAIL, and every check with CI mode
off keep running the related unit tests locally.

Real runner, Git, pytest selection and JUnit reports; only GitHub, models and
the clock are fake (the existing ci-mode world from test_ci_mode).
"""

from pathlib import Path
import re

import pytest

from conftest import git_command
from foragent import runner
from test_ci_mode import (
    LOCAL, LOCAL_ACCEPTANCE, OWN, OWN_ACCEPTANCE, mode_case,
    no_external_execution,  # noqa: F401 -- offline fixture
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


REVERIFY = "base moved: result-affecting paths; re-verifying"
MOVED = {"src/neighbor.py": "OTHER = 'moved on main after the review PASS'\n"}


def move_main_after_pass(case, *, fail_first_round=False):
    """Fake reviewer: optionally FAIL every review of the first implementation,
    then PASS; main moves during the first PASS.

    The move touches a source path the PR does not change, so publication must
    merge it and re-verify (not reuse) the verified head.
    """
    def review(kwargs):
        if fail_first_round and len(case.implementer.calls) == 1:
            return "VERDICT: FAIL\n- src/demo.py:1 VALUE must be 3"
        if not hasattr(case, "calls_before_move"):
            case.calls_before_move = len(case.processes.calls)
            case.history.update(MOVED)
            case.base_sha = case.history.tip  # CI's comparison runs against the moved base.
        return "VERDICT: PASS"

    case.reviewer.steps = [review] * 8


def head_runs(case, calls):
    """Test runs on the implementation worktree (not baseline captures elsewhere)."""
    return [call for call in calls if call.cwd == case.store.worktree_path(1) and not call.capture]


def executed(calls):
    return set().union(*(call.executed for call in calls)) if calls else set()


def lines_after_reverify(case):
    log = case.store.log_path(1).read_text()
    assert REVERIFY in log, "Main moved on a result-affecting path after the PASS: publication must re-verify"
    return log[log.index(REVERIFY):].splitlines()[1:]


def left_to_ci_lines(lines):
    return [line for line in lines
            if "related" in line.lower() and re.search(r"\bci\b", line, re.IGNORECASE)]


def assert_merged_on_moved_base(case, outcome):
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert case.tracker.merged_prs == [2]
    pushed = git_command(case.history.origin, "show", f"foragent/issue-1:{next(iter(MOVED))}")
    assert pushed == MOVED[next(iter(MOVED))].strip(), "The pushed head carries the moved base"
    head = case.tracker.pr_head_sha(2)
    assert git_command(case.history.origin, "rev-parse", "foragent/issue-1") == head


def run_local_mode_world(git_history, tmp_path):
    """The same post-PASS move with CI mode off, in its own copy of the offline history."""
    import shutil
    from conftest import GitHistory

    root = tmp_path / "local-mode"
    root.mkdir()
    origin = shutil.copytree(git_history.origin, root / "origin.git")
    clone = shutil.copytree(git_history.clone, root / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    history = GitHistory(origin, clone, git_history.tip)
    with pytest.MonkeyPatch.context() as patch:
        case = mode_case(history, root, patch, mode="local")
        move_main_after_pass(case)
        outcome = runner.run_issue(1, case.ctx)
        assert outcome == "merged", case.store.log_path(1).read_text()
        after = head_runs(case, case.processes.calls[case.calls_before_move:])
        assert OWN in executed(after), (
            "With CI mode off, the re-verify after a post-PASS base move still runs the related unit tests"
        )
        assert not left_to_ci_lines(lines_after_reverify(case)), (
            "With CI mode off nothing is left to PR CI"
        )


def test_ci_mode_post_pass_base_refresh_leaves_related_units_to_pr_ci(git_history, tmp_path, monkeypatch):
    # Guard first: CI mode off keeps today's local related-unit run on the same move.
    run_local_mode_world(git_history, tmp_path)

    (tmp_path / "ci-mode").mkdir()
    case = mode_case(git_history, tmp_path / "ci-mode", monkeypatch)
    move_main_after_pass(case)

    outcome = runner.run_issue(1, case.ctx)

    assert_merged_on_moved_base(case, outcome)
    before = head_runs(case, case.processes.calls[:case.calls_before_move])
    after = head_runs(case, case.processes.calls[case.calls_before_move:])
    assert OWN in executed(before), "The first check before review still runs the related unit tests locally"
    assert after, "The post-PASS base move must re-verify on this machine"
    ran = executed(after)
    assert OWN_ACCEPTANCE in ran, "The re-verify still runs Done when on the merged head"
    assert LOCAL in ran, "The re-verify still runs the local_only unit tests"
    assert LOCAL_ACCEPTANCE in ran, "The re-verify still checks the protected (local_only) acceptance tests"
    assert OWN not in ran, (
        "In CI mode the post-PASS base-refresh re-verify must leave the related own unit files to PR CI; "
        f"observed {sorted(ran)}"
    )
    assert not any(any(Path(word).name == Path(OWN.split("::")[0]).name for word in call.words)
                   for call in after), "No related unit file may be started locally after the PASS"
    lines = left_to_ci_lines(lines_after_reverify(case))
    assert len(lines) == 1, (
        "Record exactly one plain line that the related unit tests are left to PR CI on the new head; "
        f"observed {lines}"
    )
    head = case.tracker.pr_head_sha(2)
    assert any(read_head == head for read_head, _ in case.ci.reads), (
        "Merge still needs CI evidence for the exact pushed head"
    )


def test_ci_mode_check_after_review_fail_still_runs_related_units(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch)

    def repair(kwargs):
        case.calls_before_repair = len(case.processes.calls)
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        return "Addressed the review finding"

    case.implementer.steps = [case.implement, repair]
    move_main_after_pass(case, fail_first_round=True)

    outcome = runner.run_issue(1, case.ctx)

    assert_merged_on_moved_base(case, outcome)
    assert len(case.implementer.calls) == 2, "One repair turn after the review FAIL"
    assert case.calls_before_repair < case.calls_before_move, "Main moves only after the repaired head's PASS"
    repaired = head_runs(case, case.processes.calls[case.calls_before_repair:case.calls_before_move])
    after = head_runs(case, case.processes.calls[case.calls_before_move:])
    assert OWN in executed(repaired), "The check after a review FAIL still runs the related unit tests locally"
    assert after and OWN not in executed(after), (
        "Only the post-PASS base-refresh re-verify leaves related units to PR CI"
    )
    assert len(left_to_ci_lines(lines_after_reverify(case))) == 1

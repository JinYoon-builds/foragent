"""Acceptance test for conflict-recheck-overlap (re-check). Locked by Foragent; do not edit.

#1641 (2026-11-04/06): a reviewed PR waited to merge, main moved and conflicted,
and the run redid a full multi-perspective review and every related local test
although only the merge resolution was new.

Statement: after such a resolution, the local re-check runs only the tests related
to files the resolution or main's new commits touched; the PR's other related
unit tests are left out (PR CI still runs everything).

Real runner, Git, pytest selection and JUnit reports (the ci-mode world). Only GitHub,
models and the clock are fake. The conflict comes from main moving during the
first review, so the existing publication path meets a real conflict.
"""

import pytest

from conftest import git_command
from foragent import runner
from test_ci_mode import (
    OWN, mode_case,
    no_external_execution,  # noqa: F401 -- offline fixture
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


ALPHA_TEST = "tests/test_alpha.py::test_alpha"


def head_runs(case, calls):
    return [call for call in calls if call.cwd == case.store.worktree_path(1) and not call.capture]


def executed(calls):
    return set().union(*(call.executed for call in calls)) if calls else set()


@pytest.mark.parametrize("mode", ["local", "ci"])
def test_recheck_after_conflict_runs_only_tests_related_to_resolved_or_main_touched_files(
    git_history, tmp_path, monkeypatch, mode,
):
    # A second module the PR changes that neither main nor the resolution touches.
    git_history.update({
        "src/alpha.py": "ALPHA = 1\n",
        "tests/test_alpha.py": "from src.alpha import ALPHA\n\ndef test_alpha():\n    assert ALPHA > 0\n",
    })
    case = mode_case(git_history, tmp_path, monkeypatch, mode=mode)
    implement_demo = case.implement

    def first(kwargs):
        implement_demo(kwargs)  # src/demo.py: VALUE = 2
        (kwargs["worktree"] / "src/alpha.py").write_text("ALPHA = 2\n")
        return "Implemented demo and alpha"

    def resolve(kwargs):
        path = kwargs["worktree"] / "src/demo.py"
        assert "<<<<<<<" in path.read_text(), "Exercise a real conflict with main"
        case.calls_at_resolution = len(case.processes.calls)
        path.write_text("VALUE = 7\n")  # only the conflicting line
        return "Resolved the conflict in src/demo.py"

    case.implementer.steps = [first, resolve]

    def review(kwargs):
        if not hasattr(case, "calls_before_move"):
            case.calls_before_move = len(case.processes.calls)
            # Main's new commit conflicts on src/demo.py only.
            case.history.update({"src/demo.py": "VALUE = 5\n"})
            case.base_sha = case.history.tip  # CI's comparison runs against the moved base.
        return "VERDICT: PASS"

    case.reviewer.steps = [review] * 8
    outcome = runner.run_issue(1, case.ctx)

    log = case.store.log_path(1).read_text()
    assert outcome == "merged", log[-3000:]
    assert len(case.implementer.calls) == 2, "One lane turn resolves the conflict"
    before = head_runs(case, case.processes.calls[:case.calls_before_move])
    assert ALPHA_TEST in executed(before), "Guard: the first check runs the PR's related alpha tests"
    assert OWN in executed(before)
    after = head_runs(case, case.processes.calls[case.calls_at_resolution:])
    ran = executed(after)
    assert after, "The resolution must be re-checked on this machine"
    assert OWN in ran, "Tests related to the resolved file (src/demo.py) still run locally"
    assert ALPHA_TEST not in ran, (
        "The re-check after the conflict runs only tests related to files the resolution or main's new "
        f"commits touched; src/alpha.py was touched by neither, observed {sorted(ran)}"
    )
    assert not any(any(word.endswith("test_alpha.py") for word in call.words) for call in after), (
        "No test file related only to untouched PR files may be started after the resolution"
    )
    pushed = git_command(case.history.origin, "show", "foragent/issue-1:src/demo.py")
    assert pushed == "VALUE = 7", "The resolved head is published"
    assert git_command(case.history.origin, "show", "foragent/issue-1:src/alpha.py") == "ALPHA = 2"

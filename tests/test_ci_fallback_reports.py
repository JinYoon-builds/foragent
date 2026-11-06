"""A local fallback is replaced when CI reports appear before the merge lock (#1641 review)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_ci_mode import KEEP, comparison_record, mode_case, no_external_execution  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401
from test_state_split import offline_acceptance  # noqa: E402,F401


def test_reports_that_appear_after_a_local_fallback_return_their_regression_to_the_worker(
    git_history, tmp_path, monkeypatch,
):
    from foragent import ci_local
    from foragent.runner import run_issue

    case = mode_case(git_history, tmp_path, monkeypatch, fallback="local")
    case.ci.damage = "missing"
    measured = ci_local.fallback_comparison

    def fallback_then_ci_arrives(attempt, base_sha):
        result = measured(attempt, base_sha)
        # CI finishes after the local measurement: its reports show KEEP lost on the head.
        case.ci.damage, case.ci.regression = None, "nonpassing"
        return result

    monkeypatch.setattr(ci_local, "fallback_comparison", fallback_then_ci_arrives)

    def repair(kwargs):
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        case.ci.repaired = True
        return "Repaired the reported acceptance regression"

    case.implementer.steps.append(repair)
    case.reviewer.steps.append("VERDICT: PASS")
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == 2, "The lost test in the new CI reports must reach the worker"
    assert KEEP in case.implementer.calls[1]["prompt"]
    comparison_record(case, "ci")

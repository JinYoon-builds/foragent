"""Locked budget batching: rejects dropped coverage, slot overflow and local-only counting."""

from dataclasses import replace
import pytest
from foragent import capacity
from foragent.runner import run_issue
from test_review_perspective_fanout import (FirstRound, PERSPECTIVE_COUNT, assert_first_round,
                                          direction_case, install_review)
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("budget", [1, 2], ids=["sequential", "two-at-a-time"])
def test_short_machine_budget_batches_all_perspectives_without_dropping_any(
    git_history, tmp_path, monkeypatch, budget,
):
    case = direction_case(git_history, tmp_path, monkeypatch)
    # Allow more reviewers in project settings than the actual machine can run.
    case.config = case.ctx.config = replace(case.config, review_reviewers=9)
    capacity.write_state(case.store, budget, case.clock[0])
    panel = FirstRound(case, ["VERDICT: PASS"] * PERSPECTIVE_COUNT, "VERDICT: PASS",
                       overlap=budget, accounted=True)
    install_review(case, panel.run)
    outcome = run_issue(1, case.ctx)
    assert not panel.errors, panel.errors
    assert_first_round(panel)
    assert panel.maximum == budget, "Use the available budget, then continue the remaining perspectives"
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
    assert capacity.machine_runs(case.store, case.clock[0]) == 0, "Release all reviewer capacity"

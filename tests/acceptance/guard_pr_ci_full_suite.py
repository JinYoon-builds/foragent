"""Regression guard for pr-ci-related-only; not part of Done when (passes on main).

The merge-train tip, a push to main, and a lane PR that changes a CI workflow or the test
harness all run the full suite and succeed. On main every PR already runs everything, so
these only catch an implementation that narrows too much.
"""

import pytest

from pr_ci_world import TOUCH_ALPHA, TOUCH_CI_WORKFLOW, TOUCH_HARNESS, Scenarios, assert_full_suite


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return Scenarios(tmp_path_factory)


@pytest.mark.parametrize("kind", ["train", "push"])
def test_train_tip_and_main_push_run_the_full_suite(scenarios, kind):
    assert_full_suite(scenarios.run(kind, TOUCH_ALPHA, "module"))


@pytest.mark.parametrize("label, change", [("ci-workflow", TOUCH_CI_WORKFLOW), ("test-harness", TOUCH_HARNESS)])
def test_lane_pr_with_unmappable_change_runs_the_full_suite(scenarios, label, change):
    assert_full_suite(scenarios.run("lane", change, label))

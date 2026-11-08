"""Acceptance for pr-ci-related-only (Jin 11-08, r1227, CI capacity B).

The project's CI (committed workflows plus `fa ci workflow` output) runs in an offline
GitHub Actions world (see pr_ci_world.py). What counts is which test files pytest
actually executed for one event:

- a lane PR (head `foragent/issue-7`) runs only the tests related to its changed files
  plus its own Done when test, not the full suite;
- a merge-train PR (`foragent/batch-merge-1`) and a push to main still run the full suite,
  where the lane PR for the same change does not;
- a lane PR that changes a CI workflow or the test harness runs the full suite, where a
  lane PR that changes only a module does not.

Each test here fails on current main, where every PR runs the full suite. Regression
controls that already pass on main live in guard_pr_ci_full_suite.py.
"""

import pytest

from pr_ci_world import (
    ACCEPT_ALPHA, ACCEPT_BETA, ALL_TESTS, DONE_WHEN_FILE, TOUCH_ALPHA, TOUCH_BETA_TEST, TOUCH_CI_WORKFLOW,
    TOUCH_HARNESS, UNIT_ALPHA, UNIT_BETA, UNIT_DELTA, UNIT_GAMMA, Scenarios, assert_full_suite, assert_succeeded,
)


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return Scenarios(tmp_path_factory)


# (label, change, tests the lane PR must execute, tests it must not execute)
LANE_CHANGES = [
    pytest.param("module", TOUCH_ALPHA, {UNIT_ALPHA}, {UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_BETA},
                 id="changed-module"),
    pytest.param("own-test", TOUCH_BETA_TEST, {UNIT_BETA},
                 {UNIT_ALPHA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_ALPHA, ACCEPT_BETA},
                 id="changed-test-file"),
]


@pytest.mark.parametrize("label, change, related, unrelated", LANE_CHANGES)
def test_lane_pr_runs_only_related_tests_plus_its_done_when(scenarios, label, change, related, unrelated):
    """Reject the full suite on a lane PR, a selection that drops the Done when, and a wrong selection."""
    outcome = scenarios.run("lane", change, label)
    assert_succeeded(outcome)
    missing = sorted((related | {DONE_WHEN_FILE}) - outcome.files)
    assert not missing, (
        "A lane PR's CI must run the unit tests related to its changed files and its own Done when "
        f"test; never executed: {missing}\n{outcome.describe()}")
    extra = sorted(unrelated & outcome.files)
    assert not extra, (
        "A lane PR's CI must not run tests unrelated to its changed files (only related tests plus "
        f"its Done when); executed anyway: {extra}\n{outcome.describe()}")
    assert outcome.files < ALL_TESTS, f"A lane PR must not run the full suite.\n{outcome.describe()}"


@pytest.mark.parametrize("kind", ["train", "push"])
def test_train_and_main_run_the_full_suite_where_the_lane_pr_does_not(scenarios, kind):
    """The same change: full suite on the train tip and on main, related-only on the lane PR."""
    full = scenarios.run(kind, TOUCH_ALPHA, "module")
    assert_full_suite(full)
    lane = scenarios.run("lane", TOUCH_ALPHA, "module")
    assert_succeeded(lane)
    skipped = sorted(full.files - lane.files)
    assert {UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_BETA} <= set(skipped), (
        f"The {kind} CI runs the full suite; the lane PR for the same change must run fewer tests "
        f"(not {UNIT_BETA}, {UNIT_GAMMA}, {UNIT_DELTA} or {ACCEPT_BETA}).\nTrain/main: {sorted(full.files)}\n{lane.describe()}")
    assert DONE_WHEN_FILE in lane.files and UNIT_ALPHA in lane.files, lane.describe()


@pytest.mark.parametrize("label, change", [
    pytest.param("ci-workflow", TOUCH_CI_WORKFLOW, id="ci-workflow"),
    pytest.param("test-harness", TOUCH_HARNESS, id="test-harness"),
])
def test_lane_pr_changing_ci_or_harness_runs_full_suite_unlike_a_module_change(scenarios, label, change):
    """Unmappable changes widen to the full suite; a plain module change still narrows."""
    unmappable = scenarios.run("lane", change, label)
    assert_full_suite(unmappable)
    narrow = scenarios.run("lane", TOUCH_ALPHA, "module")
    assert_succeeded(narrow)
    assert not {UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_BETA} & narrow.files, (
        "Only a CI workflow or test harness change may widen a lane PR to the full suite; a lane PR "
        f"that changes only src/demo/alpha.py must stay narrow.\n{narrow.describe()}")

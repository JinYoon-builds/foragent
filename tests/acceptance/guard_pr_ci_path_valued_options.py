"""Regression guard for #2772 p4; not part of Done when (passes on main).

A lane PR whose check table holds a related-check command with path-valued pytest options
still gets a CI that succeeds and runs the related test and the Done when test.
"""

import pytest

from path_option_ci_world import LABELS, PathOptionScenarios
from pr_ci_world import DONE_WHEN_FILE, TOUCH_ALPHA, UNIT_ALPHA, assert_succeeded


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return PathOptionScenarios(tmp_path_factory)


@pytest.mark.parametrize("label", LABELS)
def test_path_option_rows_keep_ci_green(scenarios, label):
    outcome = scenarios.run(label, TOUCH_ALPHA)
    assert_succeeded(outcome)
    assert {UNIT_ALPHA, DONE_WHEN_FILE} <= outcome.files, outcome.describe()

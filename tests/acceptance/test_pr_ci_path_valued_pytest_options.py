"""Acceptance for #2772 promise p4 (r1320 round 1, Jin r1495).

Statement: a related-check command that passes a path-valued pytest option (for example
`--junitxml /tmp/x.xml` or `-o cache_dir=/tmp/c`) runs normally; the option's value is
not counted as a missing test selector.

A lane PR changes only `src/demo/alpha.py`. Its check table lists a related-check command
for `tests/test_alpha.py` with path-valued options (path_option_ci_world.py) and its Done
when. The PR's CI must succeed and run only the related test plus the Done when test:

- reading a value as a missing selector and falling back to the full suite fails (extra files);
- passing a value to pytest as a test path fails (a job fails on a missing file, or `.`
  runs everything);
- dropping the related-check row's test fails (tests/test_alpha.py never runs).

Fails on current main, where every lane PR runs the full suite.
"""

import pytest

from path_option_ci_world import LABELS, PathOptionScenarios
from pr_ci_world import (
    ACCEPT_BETA, ALL_TESTS, DONE_WHEN_FILE, TOUCH_ALPHA, UNIT_ALPHA, UNIT_BETA, UNIT_DELTA, UNIT_GAMMA,
    assert_succeeded,
)


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return PathOptionScenarios(tmp_path_factory)


@pytest.mark.parametrize("label", LABELS)
def test_path_valued_option_is_not_a_missing_selector(scenarios, label):
    outcome = scenarios.run(label, TOUCH_ALPHA)
    assert_succeeded(outcome)
    unrelated = sorted({UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_BETA} & outcome.files)
    assert not unrelated, (
        "A path-valued pytest option's value in a related-check command is not a test selector: "
        "the lane PR must not fall back to the full suite or select more because of it; "
        f"executed unrelated tests: {unrelated}\n{outcome.describe()}")
    assert outcome.files < ALL_TESTS, f"A module-only lane PR must not run the full suite.\n{outcome.describe()}"
    missing = sorted({UNIT_ALPHA, DONE_WHEN_FILE} - outcome.files)
    assert not missing, (
        "The related-check command must still run normally: its related test and the Done when "
        f"test must execute; never executed: {missing}\n{outcome.describe()}")

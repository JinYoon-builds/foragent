"""Acceptance for #2772 r1320: harness modules under src/ run the full suite.

Statement: a pull request that changes a source module the test or CI harness itself
runs (for example a pytest plugin, the CI measurement or sharding modules) runs the full
suite, not only related tests. When unsure whether a module is part of the harness, the
full suite runs.

The demo project (harness_module_ci_world.py) has four such modules under src/: a pytest
plugin loaded by `addopts -p`, a pytest plugin loaded by the root conftest's
`pytest_plugins`, a helper only that plugin imports, and a module a committed CI workflow
step runs. No test imports them. Each lane PR changing one of them must run every test
file. The same world still narrows a lane PR that changes only an application module, so
"always run the full suite" does not pass.
"""

import pytest

from harness_module_ci_world import (
    CI_STEP_MODULE, PLUGIN_ADDOPTS, PLUGIN_CONFTEST, PLUGIN_SUPPORT, TOUCH_CI_STEP_MODULE, TOUCH_PLUGIN_ADDOPTS,
    TOUCH_PLUGIN_CONFTEST, TOUCH_PLUGIN_SUPPORT, HarnessScenarios,
)
from pr_ci_world import (
    ACCEPT_BETA, DONE_WHEN_FILE, TOUCH_ALPHA, UNIT_ALPHA, UNIT_BETA, UNIT_DELTA, UNIT_GAMMA, assert_full_suite,
    assert_succeeded,
)


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return HarnessScenarios(tmp_path_factory)


HARNESS_CHANGES = [
    pytest.param("plugin-addopts", TOUCH_PLUGIN_ADDOPTS, PLUGIN_ADDOPTS, id="pytest-plugin-addopts"),
    pytest.param("plugin-conftest", TOUCH_PLUGIN_CONFTEST, PLUGIN_CONFTEST, id="pytest-plugin-conftest"),
    pytest.param("plugin-support", TOUCH_PLUGIN_SUPPORT, PLUGIN_SUPPORT, id="module-the-plugin-imports"),
    pytest.param("ci-step-module", TOUCH_CI_STEP_MODULE, CI_STEP_MODULE, id="module-a-ci-step-runs"),
]


def assert_narrow(narrow):
    assert_succeeded(narrow)
    assert {UNIT_ALPHA, DONE_WHEN_FILE} <= narrow.files, narrow.describe()
    extra = sorted({UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_BETA} & narrow.files)
    assert not extra, (
        "Only harness modules may widen a lane PR: in the same project a lane PR that changes only "
        f"the application module src/demo/alpha.py must stay related-only; ran unrelated {extra}\n"
        f"{narrow.describe()}")


@pytest.mark.parametrize("label, change, path", HARNESS_CHANGES)
def test_lane_pr_changing_a_harness_module_runs_full_suite_unlike_an_application_module(
        scenarios, label, change, path):
    """A harness module under src/ is unmappable and widens to every test file; alpha.py does not."""
    outcome = scenarios.run("lane", change, label)
    assert outcome.changed == [path], outcome.describe()
    assert_full_suite(outcome)
    assert_narrow(scenarios.run("lane", TOUCH_ALPHA, "module"))

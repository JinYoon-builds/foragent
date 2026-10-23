"""Acceptance test for ci-trusted-checks. Locked by Foragent; do not edit."""

from dataclasses import replace

import pytest
import yaml

from test_ci_feedback import LOG
from test_fast_checks import RELATED, fast_case, selected_test_files, unit_commands_after_acceptance
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


# D81: `.foragent.yml` key `runner_unit_tests`; `auto` (default) decides from the
# repository's CI, `ci` trusts CI, `all` runs every related unit test.
KEY = "runner_unit_tests"
PR_WORKFLOW = (
    "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\n"
    "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: uv run pytest -q --ignore=tests/acceptance\n"
)
PUSH_ONLY_WORKFLOW = (
    "name: ci\non:\n  push:\n    branches: [main]\n"
    "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: uv run pytest -q --ignore=tests/acceptance\n"
)
# More tests that merely import the widely used module; today's selection picks
# all of them together with RELATED.
IMPORTERS = {
    f"tests/test_scheduler_user_{index}.py": (
        "from foragent.scheduler import tick\n\n"
        f"def test_user_{index}():\n    assert callable(tick)\n"
    )
    for index in range(5)
}
OWN_TEST = "tests/test_scheduler.py"  # The changed module's own test file (also in RELATED).
CHANGED_TEST = "tests/test_changed.py"
EVERY_RELATED = set(RELATED) | set(IMPORTERS) | {CHANGED_TEST}
CHANGES = {
    "src/foragent/scheduler.py": "def tick(ctx):\n    return (ctx, 'changed')\n",
    CHANGED_TEST: "def test_changed():\n    assert 2 == 2\n",
}


def ci_case(git_history, tmp_path, monkeypatch, *, workflow, setting=None):
    """test_fast_checks' real runner case, on a base with or without a CI workflow."""
    files = dict(IMPORTERS)
    if workflow is not None:
        files[".github/workflows/ci.yml"] = workflow
    git_history.update(files)
    case = fast_case(git_history, tmp_path, monkeypatch, CHANGES)
    assert getattr(case.config, KEY) == "auto", "The default must be the automatic choice"
    if setting is not None:
        case.config = replace(case.config, **{KEY: setting})
        case.ctx.config = case.config
    return case


def unit_selection(case):
    from foragent.runner import run_issue

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    # Also asserts Done when and the unchanged acceptance baseline ran first.
    return selected_test_files(unit_commands_after_acceptance(case))


def test_ci_trusted_checks_pull_request_ci_runs_only_the_touched_files_own_tests(
    git_history, tmp_path, monkeypatch,
):
    case = ci_case(git_history, tmp_path, monkeypatch, workflow=PR_WORKFLOW)
    selected = unit_selection(case)
    assert selected == {OWN_TEST, CHANGED_TEST}, (
        "With pull_request CI the runner must leave importer-only tests to CI and run only "
        f"the changed module's own test file and changed test files: {selected}"
    )


@pytest.mark.parametrize("workflow", [None, PUSH_ONLY_WORKFLOW], ids=["no-workflow", "push-only"])
def test_ci_trusted_checks_without_pull_request_ci_runs_every_related_test(
    git_history, tmp_path, monkeypatch, workflow,
):
    case = ci_case(git_history, tmp_path, monkeypatch, workflow=workflow)
    selected = unit_selection(case)
    assert selected == EVERY_RELATED, f"Without PR CI every related unit test must run: {selected}"
    assert "tests/test_scheduler_import.py" in selected  # Imports the module; not its own test file.


def test_ci_trusted_checks_setting_all_runs_every_related_test_even_with_ci(
    git_history, tmp_path, monkeypatch,
):
    case = ci_case(git_history, tmp_path, monkeypatch, workflow=PR_WORKFLOW, setting="all")
    selected = unit_selection(case)
    assert selected == EVERY_RELATED, f"`{KEY}: all` must run every related unit test: {selected}"


def test_ci_trusted_checks_setting_ci_trusts_ci_without_a_detected_workflow(
    git_history, tmp_path, monkeypatch,
):
    case = ci_case(git_history, tmp_path, monkeypatch, workflow=None, setting="ci")
    selected = unit_selection(case)
    assert selected == {OWN_TEST, CHANGED_TEST}, f"`{KEY}: ci` must leave importer-only tests to CI: {selected}"


def test_ci_trusted_checks_setting_loads_from_team_config_and_rejects_unknown_values(tmp_path):
    from foragent.config import ConfigError, load_config

    path = tmp_path / ".foragent.yml"
    no_global = tmp_path / "missing-global.yml"
    path.write_text(yaml.safe_dump({"review": "batch"}), encoding="utf-8")
    assert getattr(load_config(tmp_path, no_global), KEY) == "auto"
    for value in ("auto", "ci", "all"):
        path.write_text(yaml.safe_dump({"review": "batch", KEY: value}), encoding="utf-8")
        assert getattr(load_config(tmp_path, no_global), KEY) == value
    for value in ("some", "", True, None):
        path.write_text(yaml.safe_dump({"review": "batch", KEY: value}), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(tmp_path, no_global)


@pytest.mark.parametrize("setting", ["auto", "ci", "all"])
def test_ci_trusted_checks_every_setting_keeps_baseline_and_green_ci_before_merge(
    git_history, tmp_path, monkeypatch, setting,
):
    """The D53 baseline still runs and failing PR CI still goes back to the worker (D50)."""
    from foragent.runner import run_issue

    case = ci_case(git_history, tmp_path, monkeypatch, workflow=PR_WORKFLOW, setting=setting)
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, "setup ok\n" + LOG + "\n")
    first = case.implementer.steps[0]

    def fix(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done, CI fixed\n")
        case.tracker.set_pr_checks(2, "pass")
        return "Fixed CI"

    case.implementer.steps = [first, fix]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    unit_commands_after_acceptance(case)  # Baseline ran with the acceptance ids.
    assert len(case.implementer.calls) == 2, "Red CI must not merge; the worker gets the failure"
    assert LOG in case.implementer.calls[1]["prompt"]

"""Lane PR test selection (related_ci): pytest option values and the harness never narrow wrongly."""

from pathlib import Path

import pytest

from foragent import related_ci


@pytest.fixture
def root(tmp_path):
    for relative in ("tests/test_alpha.py", "tests/acceptance/test_done.py", "src/demo/alpha.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("command", [
    "uv run pytest -q tests/test_alpha.py --junitxml /tmp/foragent-related/report.xml",
    "uv run pytest -q --junitxml /tmp/x/report.xml -o junit_family=legacy tests/test_alpha.py",
    "uv run pytest -q tests/test_alpha.py --basetemp missing/tmp -p no:cacheprovider -k 'a/b'",
    ".venv/bin/pytest -q --rootdir . -c missing/pytest.ini tests/test_alpha.py",
])
def test_option_values_are_not_selectors(root, command):
    assert related_ci.command_paths(command, root) == ["tests/test_alpha.py"]


def test_paths_outside_the_repository_are_not_selectors(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "test_other.py"
    outside.write_text("", encoding="utf-8")
    command = f"uv run pytest -q {outside} {root / 'tests/acceptance/test_done.py'}"
    assert related_ci.command_paths(command, root) == ["tests/acceptance/test_done.py"]


def test_a_missing_named_test_path_widens(root):
    assert related_ci.command_paths("uv run pytest -q tests/test_gone.py", root) is None


@pytest.mark.parametrize("path", [
    *sorted(related_ci.CI_HARNESS), "src/conftest.py", "src/demo/conftest.py", "src/sitecustomize.py",
    "tests/conftest.py", "tests/helpers.py", "pytest.ini", ".github/workflows/ci.yml", "pyproject.toml",
])
def test_harness_changes_are_unmappable(path):
    assert not related_ci.mappable(path)


@pytest.mark.parametrize("path", ["src/demo/alpha.py", "src/foragent/cli.py", "tests/test_alpha.py",
                                  "tests/acceptance/test_done.py"])
def test_modules_and_test_files_are_mappable(path):
    assert related_ci.mappable(path)


def test_lane_table_with_related_check_keeps_the_selection_narrow(root):
    body = ("| Done when command | Result |\n| --- | --- |\n"
            "| <code>uv run pytest -q tests/acceptance/test_done.py</code> | pass |\n"
            "| <code>uv run pytest -q tests/test_alpha.py --junitxml /tmp/r/alpha.xml</code> | pass |\n")
    selection = related_ci.select(root, ["src/demo/alpha.py"], related_ci.done_when_commands(body),
                                  "tests/acceptance/")
    assert selection == related_ci.Selection(("tests/test_alpha.py",), ("tests/acceptance/test_done.py",))


def test_a_harness_change_selects_the_full_suite(root):
    assert related_ci.select(root, ["src/demo/alpha.py", "src/foragent/ci_pytest.py"], [], "tests/acceptance/") is None

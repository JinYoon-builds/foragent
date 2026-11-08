"""Acceptance test for empty-group (PM r1221).

With related_check_groups at 2, a related check whose selection leaves one file group
with no selected test must not run pytest on that group as a failure (pytest exits 5
for "0 selected"). The check passes or fails only on the tests that are selected.

Real pytest runs on a small temporary tree through the public related-check entry
point (run_checks.run_related_check, shared by every verification path). Each small
test appends its name to a marker file when it really executes, so the tests see
what ran and how often. No signal is sent; no network is used.
"""

import sys

import pytest

from conftest import BASE_TIME
from foragent.config import Config
from foragent.git import GitRepo
from foragent.run_checks import _pytest_command, run_related_check
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker

COVERED = "tests/test_covered.py"
OPEN = "tests/test_open.py"
HELPERS = "tests/test_helpers_only.py"
COVERED_IDS = {f"{COVERED}::test_covered_one", f"{COVERED}::test_covered_two"}


def _marking(marks, name, body="    pass\n"):
    return (f"def {name}():\n"
            f"    with open({str(marks)!r}, 'a') as stream:\n"
            f"        stream.write({name!r} + '\\n')\n" + body)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="test", worktree=root)
    # main's config holds the stopgap related_check_groups: 1; this world sets 2 explicitly.
    config = Config(checks="ci", merge_owner=True, related_check_groups=2)
    ctx = RunContext("owner/repo", root, FakeTracker(), store, config, GitRepo(root), lambda: BASE_TIME)
    command = _pytest_command(f"{sys.executable} -m pytest")
    marks = tmp_path / "marks.txt"
    return root, ctx, command, marks


def _ran(marks):
    counts = {}
    if marks.exists():
        for name in marks.read_text().splitlines():
            counts[name] = counts.get(name, 0) + 1
    return counts


def _write_covered(root, marks):
    (root / COVERED).write_text(
        _marking(marks, "test_covered_one", "    assert False, 'CI already passed this'\n") + "\n\n"
        + _marking(marks, "test_covered_two", "    assert False, 'CI already passed this'\n"))


def _record(ctx):
    records = ctx.store.get_run(1).related_tests
    assert len(records) == 1, f"one related check must leave exactly one record: {records}"
    return records[0]


def test_group_left_empty_by_ci_passes_is_skipped_and_the_check_passes(world):
    root, ctx, command, marks = world
    _write_covered(root, marks)
    (root / OPEN).write_text(_marking(marks, "test_open"))

    result = run_related_check(1, ctx, root, command, [COVERED, OPEN], 2, 120,
                               ci_passed=(42, set(COVERED_IDS)), head_sha="exact-head")

    assert result.exit_code == 0 and not result.timed_out, (
        "A group whose every test CI already passed must be skipped, never a failure "
        f"(pytest exit 5 is not a failure): exit={result.exit_code}\n{result.output}")
    assert _ran(marks) == {"test_open": 1}, (
        f"The selected test must run exactly once and CI-passed tests not at all: {_ran(marks)}")
    record = _record(ctx)
    assert record["selected_files"] == sorted([COVERED, OPEN])
    assert record["timed_out"] is False
    assert OPEN in record["file_seconds"], f"per-file seconds must still be recorded: {record}"
    assert isinstance(record["total_seconds"], (int, float)) and record["total_seconds"] >= 0


def test_failing_selected_test_still_fails_when_another_group_is_empty(world):
    root, ctx, command, marks = world
    _write_covered(root, marks)
    (root / OPEN).write_text(_marking(marks, "test_open", "    assert False, 'open test really fails'\n"))

    result = run_related_check(1, ctx, root, command, [COVERED, OPEN], 2, 120,
                               ci_passed=(42, set(COVERED_IDS)), head_sha="exact-head")

    assert result.exit_code not in (0, 5), (
        "The check's result must come from the group that ran (its real test failure), "
        f"not from an empty group's pytest exit 5: exit={result.exit_code}\n{result.output}")
    assert "open test really fails" in result.output
    assert _ran(marks) == {"test_open": 1}, _ran(marks)
    _record(ctx)


def test_file_without_selected_tests_does_not_fail_a_grouped_local_check(world):
    root, ctx, command, marks = world
    # Two related files are selected, but only one holds any test.
    (root / HELPERS).write_text("VALUE = 1\n\n\ndef helper():\n    return VALUE\n")
    (root / OPEN).write_text(_marking(marks, "test_open"))

    result = run_related_check(1, ctx, root, command, [HELPERS, OPEN], 2, 120)

    assert result.exit_code == 0 and not result.timed_out, (
        "With grouping on, a group holding no selected test must be skipped like a serial check "
        f"would pass: exit={result.exit_code}\n{result.output}")
    assert _ran(marks) == {"test_open": 1}, _ran(marks)
    record = _record(ctx)
    assert record["selected_files"] == sorted([HELPERS, OPEN])
    assert OPEN in record["file_seconds"], record

"""Regression controls for stale-base-main-red (2026-11-09, PM r1433).

These already pass on main bed981fe and stay outside Done when. They keep the stale-base rule from
silencing real failures on main's latest measured commit, keep #2951's rule that a later
measurement which did not run the test changes nothing, keep the shapes of statement 1 that main
already handles by commit order, and keep plain closing working. The checks that fail on main are
in test_stale_base_main_red.py.
"""

from datetime import timedelta

import pytest

from closed_lock_every_capture_support import capture, new_commit
from closed_lock_red_support import (
    ALL_NODES, CLOSED_ISSUE, CLOSED_NODE, gate_pass, names_commit, names_issue, requests_naming,
)
from stale_base_main_red_support import (
    assert_no_entry, broken_on_main, capture_latest_elsewhere, capture_latest_green, capture_stale_failure,
    cleared_events, observe_ci,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_stale_base_main_red import STALE, world  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401


def test_failure_on_main_latest_measured_commit_opens_one_entry(world, capsys):  # noqa: F811
    case = world
    capture_latest_elsewhere(case)
    gate_pass(case, capsys)
    newest = new_commit(case)
    capture(case, newest, failing=[CLOSED_NODE])
    gate_pass(case, capsys)
    gate_pass(case, capsys)
    assert CLOSED_NODE in broken_on_main(case, capsys)
    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1 and names_issue(rows[0], CLOSED_ISSUE) and names_commit(rows[0], newest), rows


def test_open_entry_closes_when_a_newer_full_capture_passes(world, capsys):  # noqa: F811
    case = world
    capture(case, case.latest, failing=[CLOSED_NODE])
    gate_pass(case, capsys)
    assert CLOSED_NODE in broken_on_main(case, capsys)
    capture(case, new_commit(case))
    gate_pass(case, capsys)
    assert CLOSED_NODE not in broken_on_main(case, capsys)
    gate_pass(case, capsys)
    assert len(cleared_events(case)) == 1
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 1


def test_stale_failure_newest_reading_opens_one_entry_when_later_capture_skipped_it(world, capsys):  # noqa: F811
    """#2951: a later measurement that did not run the test changes nothing."""
    case = world
    capture_stale_failure(case)
    capture_latest_elsewhere(case)
    gate_pass(case, capsys)
    gate_pass(case, capsys)
    assert CLOSED_NODE in broken_on_main(case, capsys)
    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1 and names_issue(rows[0], CLOSED_ISSUE) and names_commit(rows[0], case.stale), rows


@pytest.mark.parametrize("arrival", ["stale-read-after-green", "both-in-one-pass"])
def test_stale_failure_opens_no_entry_after_later_main_measurement_passes_it(world, capsys, arrival):
    case = world
    if arrival == "stale-read-after-green":
        capture_latest_green(case)
        gate_pass(case, capsys)
        assert_no_entry(case, capsys, why="Nothing failed yet")
        capture_stale_failure(case)
    else:
        capture_stale_failure(case)
        capture_latest_green(case)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)


def test_stale_failure_recorded_later_in_time_still_opens_no_entry(world, capsys):
    """'Older' is by ancestry: a run of the old base recorded after main's green measurement."""
    case = world
    capture_latest_green(case, created_at=case.clock[0] - timedelta(minutes=10))
    gate_pass(case, capsys)
    capture(case, case.stale, failing=[CLOSED_NODE], created_at=case.clock[0] - timedelta(minutes=1))
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)


def test_stale_base_ci_report_failure_opens_no_entry(world, capsys):
    case = world
    capture_latest_green(case)
    gate_pass(case, capsys)
    # A CI comparison whose base side measured main at the stale commit: the lock did not pass.
    observe_ci(case, case.stale, ALL_NODES, [node for node in ALL_NODES if node != CLOSED_NODE])
    assert_no_entry(case, capsys, why=STALE)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)


def test_newer_capture_that_skipped_the_test_changes_nothing(world, capsys):
    """The green measurement keeps outranking the stale failure after main moves on and a newer
    partial capture does not run the test."""
    case = world
    capture_latest_green(case)
    gate_pass(case, capsys)
    newest = new_commit(case)
    capture_latest_elsewhere(case, newest)
    gate_pass(case, capsys)
    capture_stale_failure(case)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)

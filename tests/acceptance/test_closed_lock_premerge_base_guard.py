"""Regression controls for alarm-ignores-premerge-base (2026-11-09); not in Done when.

These pass on main before the change and must keep passing: a merge commit measured green after a
red batch base raises nothing, and a red merge commit with no earlier reading is reported naming it.
"""

import pytest

from closed_lock_premerge_support import land_member
from closed_lock_red_support import (
    CLOSED_ISSUE, CLOSED_NODE, CLOSED_PASSING, gate_pass, measure, names_commit, names_issue,
    red_main, requests_naming, setup,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401


@pytest.fixture
def world(tick_case):  # noqa: F811
    case = tick_case
    case.first = setup(case)
    return case


def test_green_merge_commit_after_a_red_batch_base_raises_nothing(world, capsys):
    case = world
    red_main(case, [CLOSED_NODE, CLOSED_PASSING])
    merge = land_member(case)
    measure(case, merge, [])
    gate_pass(case, capsys)
    assert requests_naming(case, capsys, CLOSED_NODE) == []
    assert requests_naming(case, capsys, CLOSED_PASSING) == []


def test_red_merge_commit_without_earlier_readings_is_reported_naming_it(world, capsys):
    case = world
    merge = land_member(case)
    measure(case, merge, [CLOSED_NODE])
    gate_pass(case, capsys)
    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1, rows
    assert names_commit(rows[0], merge) and names_issue(rows[0], CLOSED_ISSUE), rows[0]

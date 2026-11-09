"""Regression guard for closed-lock-red-alarm (2026-11-09): passes on main and must keep passing.

Open issues' locked tests fail on main by design until their issue merges. Recorded main results
that show only such failures raise no orchestrator request, on any number of gate passes. Not
part of the issue's Done when; see closed_lock_red_support for the recorded world.
"""

import pytest

from closed_lock_red_support import OPEN_FILE, OPEN_ISSUE, OPEN_NODE, gate_pass, measure, names, names_issue, red_main, setup
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, tick_case  # noqa: F401


@pytest.fixture
def world(tick_case):  # noqa: F811
    case = tick_case
    case.first = setup(case)
    measure(case, case.first, [OPEN_NODE])
    return case


def test_open_issue_lock_failing_on_main_raises_no_request(world, capsys):
    case = world
    for _ in range(3):
        red_main(case, [OPEN_NODE])
        gate_pass(case, capsys)
    red_main(case, [])
    red_main(case, [OPEN_NODE])
    gate_pass(case, capsys)

    for row in inbox(case, capsys):
        assert not names(row, OPEN_NODE) and OPEN_FILE not in str(row), (
            f"An open issue's lock fails on main by design and raises nothing: {row}")
        assert not names_issue(row, OPEN_ISSUE), f"The open issue #{OPEN_ISSUE} is not named: {row}"

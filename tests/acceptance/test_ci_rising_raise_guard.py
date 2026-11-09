"""Regression controls for ci-rising-raise that already pass on main (not in Done when).

The first raise is unchanged, a job reaching the near-limit line still raises
ci_job_near_limit, an open request is never doubled, and a new job far above the last raise's
highest share still re-raises.
"""

from ci_rising_raise_support import (first_raise, flat, near_requests, phase, raised_jobs, rising,
                                     rising_requests, setup)
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401


def test_first_rise_is_raised_once_and_stays_one_request_while_open(tick_case, capsys):
    case = tick_case
    setup(case)
    phase(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    rows = rising_requests(case, capsys)
    assert len(rows) == 1 and raised_jobs(rows[0]) == {"test (0)"}
    # Still open: a much higher share does not add a second request.
    phase(case, capsys, {"test (0)": rising(40), "test (1)": flat()})
    assert len(rising_requests(case, capsys)) == 1


def test_new_job_far_above_the_highest_share_raises_again(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    # test (1) appears at 30%: 21 points above the highest share (9%) in the last raise.
    phase(case, capsys, {"test (0)": rising(9), "test (1)": rising(30)})
    rows = rising_requests(case, capsys)
    assert len(rows) == 2, f"A new job far above the last raise is raised again: {rows}"
    assert "test (1)" in raised_jobs(rows[-1])


def test_near_limit_still_raises_its_own_signal(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    # test (1) reaches the near-limit line (80%) while test (0) keeps rising slightly.
    phase(case, capsys, {"test (0)": rising(12), "test (1)": [10, 10, 10, 10, 10, 85]})
    near = near_requests(case, capsys)
    assert len(near) == 1, f"A job at the near-limit line raises ci_job_near_limit as today: {near}"
    assert "test (1)" in str(near[0].get("facts"))

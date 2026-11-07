"""Acceptance tests for ci-rising-raise (2026-11-08): the CI rising alarm re-raises only on a real rise.

Statement: after a ci_job_rising raise, new CI runs that keep the same jobs rising with no job's
share of its limit up by 10 points or more do not raise the signal again. Per the Goal, the signal
is raised again only when some job's percent_of_limit is at least 10 points above that job's share
at the last raise, or a job not in the last raise appears at least 10 points above the highest
share in the last raise.

Each fixture raises once, the orchestrator answers not-now (as it did for r1096-r1107 on 11-07),
then new CI runs arrive and one `fa gate tick --no-start` measures them. Helpers are in
ci_rising_raise_support.py; regression controls that already pass on main are in
test_ci_rising_raise_guard.py.

Break-it: comparing any changed job set or duration (main), a step other than 10 points
(9 or 11), comparing with the first raise instead of the last one, comparing a new job with its
own absent share instead of the highest share, or never re-raising each fail a check below.
"""

from ci_rising_raise_support import first_raise, flat, phase, raised_jobs, rising, rising_requests, setup
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401


def test_same_jobs_rising_with_a_new_job_below_the_step_stay_quiet(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    # test (0) keeps rising, 9% -> 15% (+6 points); test (1) joins at 15%, below 9 + 10.
    phase(case, capsys, {"test (0)": rising(15), "test (1)": rising(15)})
    rows = rising_requests(case, capsys)
    assert len(rows) == 1, (
        "No job's share rose 10 points and the new job is under the highest share + 10, "
        f"so ci_job_rising must not be raised again: {[raised_jobs(row) for row in rows]}")


def test_replay_of_11_07_raises_stay_quiet(tick_case, capsys):
    """r1101 -> r1106 -> r1107: the rising set changes among jobs at 11-29% of their limits."""
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": flat(), "test (2)": flat(),
                               "test (3)": rising(25), "test (5)": rising(11)})
    # r1106: test (5) stays at 11%, test (3) at 25%.
    phase(case, capsys, {"test (0)": flat(), "test (2)": flat(),
                         "test (3)": rising(25), "test (5)": rising(11)})
    # r1107: test (3) drops out, test (0) at 29% and test (2) at 24% join (highest last share 25%).
    phase(case, capsys, {"test (0)": rising(29), "test (2)": rising(24),
                         "test (3)": flat(), "test (5)": rising(11)})
    rows = rising_requests(case, capsys)
    assert len(rows) == 1, f"The 11-07 replay must not re-raise: {[raised_jobs(row) for row in rows]}"


def test_a_job_up_by_exactly_ten_points_raises_again(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    phase(case, capsys, {"test (0)": rising(19), "test (1)": flat()})
    rows = rising_requests(case, capsys)
    assert len(rows) == 2, f"test (0) rose from 9% to 19% (+10 points), so it is raised again: {rows}"
    assert raised_jobs(rows[-1]) == {"test (0)"}
    assert rows[-1]["status"] != "done"


def test_nine_points_stay_quiet_and_the_step_is_measured_from_the_raise(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat()})
    # +9 points, and a new job at 18% (highest last share 9% + 9): both under the step.
    phase(case, capsys, {"test (0)": rising(18), "test (1)": rising(18)})
    assert len(rising_requests(case, capsys)) == 1, "Nine points is not a real rise"
    # One more point on test (0) reaches 9% + 10 at the last raise: raised again, once.
    phase(case, capsys, {"test (0)": rising(19), "test (1)": rising(18)})
    rows = rising_requests(case, capsys)
    assert len(rows) == 2, f"19% is 10 points above the 9% of the last raise: {rows}"


def test_the_next_step_is_measured_from_the_latest_raise(tick_case, capsys):
    case = tick_case
    setup(case)
    first_raise(case, capsys, {"test (0)": rising(9), "test (1)": flat(), "test (2)": flat()})
    phase(case, capsys, {"test (0)": rising(20), "test (1)": flat(), "test (2)": flat()})
    rows = rising_requests(case, capsys)
    assert len(rows) == 2, f"9% -> 20% is a real rise: {rows}"
    from ci_rising_raise_support import answer_not_now

    answer_not_now(case, capsys, rows[-1]["id"])
    # Against the latest raise (20%): +6 on test (0), and a new job at 28% < 20 + 10.
    phase(case, capsys, {"test (0)": rising(26), "test (1)": rising(28), "test (2)": flat()})
    rows = rising_requests(case, capsys)
    assert len(rows) == 2, f"Compared with the latest raise nothing rose 10 points: {rows}"
    # 31% on test (0) is 11 points above the latest raise: raised again.
    phase(case, capsys, {"test (0)": rising(31), "test (1)": rising(28), "test (2)": flat()})
    assert len(rising_requests(case, capsys)) == 3

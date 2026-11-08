"""Acceptance tests for slow-ci-flakes: the plan-width and scorecard idle worlds on a slow runner.

Every child process is slowed by the same fixed delay on fake clocks (slow_children_world), so the
CI flakes of 11-07/08 replay deterministically: no test here waits for real or sends a signal.
"""

from datetime import timedelta

import pytest

import test_orch_plan
from slow_children_world import slow_children  # noqa: F401
from test_lane_queue import queue_case  # noqa: F401
from test_scorecard_flow import NOW, _approved, _budget, _card, _metric, _phased, flow  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


# Ten minutes per child: longer than any wall-clock timeout these worlds start a child under
# (git's 120 s), as a stalled child on a loaded runner can be.
DELAY_SECONDS = 600


PLAN_WIDTH_SCENARIOS = [
    "test_plan_width_lets_a_lane_run_three_disjoint_issues",
    "test_lane_width_is_only_a_cap_set_by_a_live_plan",
    "test_plan_width_never_starts_overlapping_files",
]


@pytest.mark.parametrize("scenario", PLAN_WIDTH_SCENARIOS)
def test_plan_width_scenarios_start_the_expected_issues_on_slow_children_without_signals(
        slow_children, queue_case, capsys, monkeypatch, scenario):
    world = slow_children(DELAY_SECONDS)
    # The locked scenario asserts exactly which issues run and launch.
    getattr(test_orch_plan, scenario)(queue_case, capsys, monkeypatch)
    assert world.children, "The scenario must run its real child processes in the slow world"
    assert world.signals == [], (
        f"With every child process {DELAY_SECONDS} s slow, the plan-width ticks must deliver no real "
        f"signal; a wall-clock timeout killed {len(world.signals)} child(ren) after timing out: "
        f"{sorted({tuple(argv[:2]) + (timeout,) for argv, timeout in world.timeouts})}")
    assert world.timer_errors == [], world.timer_errors


@pytest.mark.parametrize("resource", ["parallel", "test_slots"])
def test_idle_minutes_on_slow_children_equal_the_minutes_without_delay(
        slow_children, flow, capsys, resource):
    # As on CI, a signal is refused by failing the test that sends it (tests/conftest.py).
    world = slow_children(0, to_suite_guard=True)
    start = NOW - timedelta(hours=1)
    _approved(flow, 10, start)
    _budget(flow, start)
    _phased(flow, 20, start, [("checking", 60)])
    _budget(flow, NOW - timedelta(minutes=20),
            parallel=2 if resource == "parallel" else 1,
            slots=2 if resource == "test_slots" else 1)
    fast = _metric(_card(flow, capsys), "idle_minutes")
    assert fast == pytest.approx(20), "Without delay the scenario counts twenty idle minutes"
    assert world.signals == []

    world.delay = DELAY_SECONDS
    try:
        slow, cut = _metric(_card(flow, capsys), "idle_minutes"), None
    except pytest.fail.Exception as exc:  # The read was cut by a refused real signal.
        slow, cut = None, str(exc)
    assert slow == fast, (
        f"With every child process {DELAY_SECONDS} s slow the scorecard must count the same "
        f"{fast} idle minutes; it counted {slow!r}" + (f" because the read was cut: {cut}" if cut else ""))
    assert world.signals == [], f"The slow scorecard read sent real signals: {world.signals}"
    assert world.timer_errors == [], world.timer_errors

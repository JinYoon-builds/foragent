"""Regression controls for slow-ci-flakes (not in Done when; these pass on main).

They check the slow world itself: with no delay it changes nothing, and with a delay it times
out, kills and fires timers the way a slow real child would, without sending a real signal.
"""

import subprocess
import threading
import time

import pytest

import test_orch_plan
from slow_children_world import slow_children  # noqa: F401
from test_lane_queue import queue_case  # noqa: F401
from test_scorecard_flow import flow, test_idle_minutes_counts_twenty_minutes_of_unused_budget  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_slow_ci_flakes import PLAN_WIDTH_SCENARIOS


@pytest.mark.parametrize("scenario", PLAN_WIDTH_SCENARIOS)
def test_fast_world_keeps_the_plan_width_scenarios(slow_children, queue_case, capsys, monkeypatch, scenario):
    world = slow_children(0)
    getattr(test_orch_plan, scenario)(queue_case, capsys, monkeypatch)
    assert world.children and world.signals == [] and world.timeouts == []


@pytest.mark.parametrize("resource", ["parallel", "test_slots"])
def test_fast_world_keeps_the_idle_scenario(slow_children, flow, capsys, resource):
    world = slow_children(0, to_suite_guard=True)
    test_idle_minutes_counts_twenty_minutes_of_unused_budget(flow, capsys, resource)
    assert world.signals == [] and world.timeouts == []


def test_slow_child_under_a_timeout_is_killed_on_the_fake_clock_only(slow_children):
    world = slow_children(30)
    before, wall = time.monotonic(), time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(["true"], timeout=5, capture_output=True)
    assert [signal for _, _, signal in world.signals] == [9]
    assert time.monotonic() - before >= 5
    finished = subprocess.run(["true"], timeout=60, capture_output=True)
    assert finished.returncode == 0
    assert time.monotonic() - before >= 35
    assert len(world.signals) == 1
    assert time.perf_counter() - wall < 10, "The slow world never waits for real"


def test_timers_fire_on_the_fake_clock_while_a_slow_child_runs(slow_children):
    world = slow_children(30)
    fired = []
    timer = threading.Timer(20, lambda: fired.append(time.monotonic()))
    timer.start()
    late = threading.Timer(40, lambda: fired.append("late"))
    late.start()
    subprocess.run(["true"], capture_output=True)
    late.cancel()
    assert len(fired) == 1 and fired[0] != "late"


def test_suite_guard_mode_fails_the_signal_without_sending_it(slow_children):
    world = slow_children(30, to_suite_guard=True)
    with pytest.raises(pytest.fail.Exception, match="Real delivering signals are forbidden"):
        subprocess.run(["true"], timeout=5, capture_output=True)
    assert len(world.signals) == 1

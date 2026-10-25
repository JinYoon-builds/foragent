"""Acceptance test for memory-backoff. Locked by Foragent; do not edit."""

# Imported fixtures are registered by pytest and requested by parameter name.
# ruff: noqa: F401, F811

import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_adaptive_budgets import (
    adaptive_case,
    adaptive_environment,
    history,
    sample_clock,
    sensor,
    tick_slots,
)
from test_capacity_aimd import machine_repositories
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance
from test_test_slots import machine_yml

GB = 1_000_000_000
RUN_RESERVE = 3 * GB // 2
WINDOW = timedelta(seconds=30)


@pytest.fixture(autouse=True)
def memory_sensor(sensor, monkeypatch, adaptive_environment):
    """Public seam: memory_bytes() returns (total bytes, free bytes).

    Free bytes represent memory_pressure's free percentage times total memory,
    rather than an alternative OS definition such as psutil's available bytes.
    The inherited sensor supplies kernel pressure, cumulative swap-ins and load.
    """
    from foragent import capacity

    sensor.kernel = "normal"
    sensor.memory_total = 64 * GB
    sensor.memory_free = 60 * GB
    sensor.memory_reads = []

    def memory_bytes():
        value = sensor.memory_total, sensor.memory_free
        sensor.memory_reads.append(value)
        return value

    def forbidden(*args, **kwargs):
        pytest.fail("memory acceptance tests must use fake sensors, never signal or start model turns")

    def alive(pid, signum):
        assert (pid, signum) == (os.getpid(), 0), "only the fake liveness probe is allowed"

    monkeypatch.setattr(capacity, "memory_bytes", memory_bytes, raising=False)
    monkeypatch.setattr(capacity.ctypes, "CDLL", forbidden)
    monkeypatch.setattr(os, "kill", alive)
    monkeypatch.setattr(os, "killpg", forbidden)
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and Path(argv[0]).name == "git", (
            f"only offline fixture Git subprocesses are allowed: {argv!r}"
        )
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)
    return sensor


@pytest.fixture
def memory_machine(machine_repositories, memory_sensor, monkeypatch, sample_clock):
    from foragent import capacity, scheduler

    first, second = machine_repositories
    # capacity-aimd's fixture replaces this facade with a constant sample. Keep
    # its two repositories and launch fakes, but exercise the real sensor path.
    monkeypatch.setattr(scheduler, "machine_load", capacity.machine_load)
    for case in (first, second):
        # Align the repo ceiling too: the capacity-aimd helper defaults to two
        # test slots, and threshold failures must not depend on config loading.
        case.config = case.ctx.config = case.tick_ctx.config = replace(case.config, test_slots=3)
        case.tick_ctx.baseline_launcher = lambda *args, **kwargs: None
    sample_clock(first.clock)
    machine_yml(slots=3, max_parallel=8)  # No temporary floor keys.
    return SimpleNamespace(cases=(first, second), root=first.store.root.parent, sensor=memory_sensor)


def seed_budgets(scenario, *, slots=3, parallel=8):
    from foragent import capacity

    now = scenario.cases[0].clock[0]
    capacity.write_state(scenario.cases[0].store, parallel, now)
    (scenario.root / "test-slots.json").write_text(json.dumps({
        "slots": slots, "changed_at": now.isoformat(),
    }), encoding="utf-8")
    capacity.machine_load()  # Baseline counter, not a completed 30-second window.


def tick_budgets(scenario, *, seconds=30, swapins=0, kernel=None, index=0):
    from foragent import capacity, scheduler

    now = max(case.clock[0] for case in scenario.cases) + timedelta(seconds=seconds)
    for case in scenario.cases:
        case.clock[0] = now
    scenario.sensor.total += swapins
    if kernel is not None:
        scenario.sensor.kernel = kernel
    report = scheduler.tick(scenario.cases[index].tick_ctx)
    assert report.skipped is None, report
    assert all(case.implementer.calls == case.reviewer.calls == [] for case in scenario.cases)
    slots = json.loads((scenario.root / "test-slots.json").read_text(encoding="utf-8"))["slots"]
    state = capacity.read_state(scenario.cases[index].store)
    assert state is not None, "tick must persist the shared worker budget"
    return slots, state["parallel"]


@pytest.mark.parametrize("pages", [101, 900])
def test_memory_threshold_swap_backoff_requires_three_30_second_windows(memory_machine, pages):
    scenario = memory_machine
    seed_budgets(scenario)
    actual = [tick_budgets(scenario, swapins=pages) for _ in range(3)]
    assert actual == [(3, 8), (3, 8), (2, 4)], (
        f">100 swap-in pages must decrement slots and halve workers only in window 3: {actual!r}"
    )


@pytest.mark.parametrize("pages", [
    pytest.param([1] * 6, id="one-page-is-not-pressure"),
    pytest.param([100] * 6, id="100-is-not-over-100"),
    pytest.param([101, 0, 101, 0, 101, 0], id="isolated-bursts"),
    pytest.param([101, 101, 100, 101, 101, 0], id="two-window-bursts-reset-at-100"),
])
def test_memory_threshold_small_or_interrupted_swap_never_decreases(memory_machine, pages):
    scenario = memory_machine
    seed_budgets(scenario)
    actual = [tick_budgets(scenario, swapins=value) for value in pages]
    assert actual == [(3, 8)] * len(pages), (
        f"<=100 pages or fewer than three consecutive hot windows must leave budgets unchanged: {actual!r}"
    )


def test_memory_threshold_swap_streak_restarts_after_a_cool_window(memory_machine):
    scenario = memory_machine
    seed_budgets(scenario)
    pages = [101, 101, 0, 101, 101, 101]
    actual = [tick_budgets(scenario, swapins=value) for value in pages]
    assert actual == [(3, 8)] * 5 + [(2, 4)], (
        f"a cool window must reset the swap streak; the next backoff is at 180 seconds: {actual!r}"
    )


def test_memory_threshold_repeated_ticks_do_not_complete_or_reset_a_window(memory_machine):
    scenario = memory_machine
    seed_budgets(scenario)
    actual = [tick_budgets(scenario, swapins=101)]
    # Repeated ticks (including the other repository) neither manufacture more
    # 30-second windows nor erase an already observed hot window with a zero delta.
    actual += [tick_budgets(scenario, seconds=0, index=index) for index in (1, 0, 1)]
    actual += [tick_budgets(scenario, swapins=101, index=index) for index in (1, 0)]
    assert actual == [(3, 8)] * 5 + [(2, 4)], (
        f"swap streaks must be machine-wide elapsed windows, not tick or repository counts: {actual!r}"
    )


@pytest.mark.parametrize("levels", [
    pytest.param(["warn"] * 11, id="warn"),
    pytest.param(["critical"] * 11, id="critical"),
    pytest.param(["warn", "critical"] * 5 + ["warn"], id="mixed-pressure-stays-continuous"),
])
def test_memory_threshold_kernel_backoff_requires_five_continuous_minutes(memory_machine, levels):
    scenario = memory_machine
    seed_budgets(scenario)
    actual = [tick_budgets(scenario, seconds=0, kernel=levels[0])]
    actual += [tick_budgets(scenario, kernel=level) for level in levels[1:]]
    assert actual == [(3, 8)] * 10 + [(2, 4)], (
        f"warn/critical with zero swap-ins must wait until 300 seconds before backoff: {actual!r}"
    )


def test_memory_threshold_normal_kernel_resets_the_five_minute_streak(memory_machine):
    scenario = memory_machine
    seed_budgets(scenario)
    actual = [tick_budgets(scenario, seconds=0, kernel="critical")]
    actual += [tick_budgets(scenario) for _ in range(8)]  # 240 seconds, still too short.
    actual.append(tick_budgets(scenario, kernel="normal"))
    actual.append(tick_budgets(scenario, kernel="warn"))  # New streak begins at t=300.
    actual += [tick_budgets(scenario) for _ in range(10)]
    assert actual == [(3, 8)] * 20 + [(2, 4)], (
        f"separate pressure episodes must not accumulate toward five continuous minutes: {actual!r}"
    )


def test_memory_threshold_gate_status_recovers_to_ceiling_without_floors(
    adaptive_case, memory_sensor, sample_clock, capsys,
):
    from foragent import capacity

    scenario = adaptive_case
    sample_clock(scenario.case.clock)
    scenario.context.baseline_launcher = lambda *args, **kwargs: None
    capacity.machine_load()
    actual = []
    for _ in range(5):
        memory_sensor.total += 7  # Never exceeds the new swap threshold.
        actual.append(tick_slots(scenario, capsys))
    assert actual == [2, 3, 3, 3, 3], (
        f"without floor keys, harmless swap-ins must allow gate status to recover and stay at 3: {actual!r}"
    )


def test_memory_threshold_worker_budget_recovers_to_ceiling_without_floors(memory_machine):
    from foragent import capacity

    scenario = memory_machine
    seed_budgets(scenario, slots=1, parallel=1)
    actual = [tick_budgets(
        scenario, seconds=capacity.INCREASE_INTERVAL.total_seconds() + WINDOW.total_seconds(), swapins=7,
    ) for _ in range(9)]
    expected = [(2, 2), (3, 3), (3, 4), (3, 5), (3, 6), (3, 7), (3, 8), (3, 8), (3, 8)]
    assert actual == expected, (
        f"without floor keys, both budgets must recover to their machine ceilings and stay there: {actual!r}"
    )


@pytest.mark.parametrize("rule,swap_windows,pressure_windows", [
    ("sustained-swap", 3, 0), ("sustained-pressure", 0, 10),
])
def test_memory_threshold_budget_history_names_the_sustained_rule_and_window_counts(
    memory_machine, rule, swap_windows, pressure_windows,
):
    scenario = memory_machine
    seed_budgets(scenario)
    if rule == "sustained-swap":
        for _ in range(3):
            tick_budgets(scenario, swapins=101)
    else:
        tick_budgets(scenario, seconds=0, kernel="warn")
        for _ in range(10):
            tick_budgets(scenario)
    rows = history(scenario.root)  # Retain the older public transition fields.
    decreases = [row for row in rows if row["to"] < row["from"]]
    assert {row["budget"] for row in decreases} == {"test_slots", "parallel"}, (
        f"{rule} must produce shared history for both budget decreases: {rows!r}"
    )
    for row in decreases:
        assert row.get("rule") == rule, f"budget history must name the triggering rule {rule!r}: {row!r}"
        assert (row.get("swap_windows"), row.get("pressure_windows")) == (swap_windows, pressure_windows), (
            f"budget history must record consecutive completed 30-second window counts: {row!r}"
        )
    assert {(row["budget"], row["from"], row["to"]) for row in decreases} == {
        ("test_slots", 3, 2), ("parallel", 8, 4),
    }, "a sustained episode must use today's decrease, without repeated per-tick decreases"

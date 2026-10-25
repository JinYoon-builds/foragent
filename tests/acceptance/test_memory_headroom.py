"""Acceptance test for memory-headroom. Locked by Foragent; do not edit."""

# Imported fixtures are registered by pytest and requested by parameter name.
# ruff: noqa: F401, F811

import json
import os

import pytest
from test_capacity_aimd import occupy_machine_slots
from test_memory_backoff import (
    GB,
    RUN_RESERVE,
    adaptive_environment,
    isolated_home,
    machine_repositories,
    memory_machine,
    memory_sensor,
    offline_acceptance,
    sample_clock,
    seed_budgets,
    sensor,
    tick_budgets,
)


@pytest.mark.parametrize("held", ["live-runs", "reserved-starts"])
@pytest.mark.parametrize("total,offset,expected", [
    pytest.param(20 * GB, -1, 0, id="one-byte-below"),
    pytest.param(20 * GB, 0, 1, id="equal-is-admissible"),
    pytest.param(40 * GB, 1, 1, id="above-scales-with-total"),
])
def test_memory_threshold_headroom_counts_other_repositories_and_reservations(
    memory_machine, held, total, offset, expected,
):
    scenario = memory_machine
    first, second = scenario.cases
    seed_budgets(scenario)
    if held == "live-runs":
        occupy_machine_slots(first, (1, 2), pid=os.getpid())
    else:
        for number in (1, 2):
            first.store.prepare_run_start(number)
    scenario.sensor.memory_total = total
    scenario.sensor.memory_free = total // 4 + 2 * RUN_RESERVE + offset
    budgets = tick_budgets(scenario, index=1)
    assert len(second.launched) == expected, (
        "headroom must apply free_bytes - 1.5 GB * machine running/reserved >= total_bytes / 4 "
        f"before each admission ({held}, offset {offset}); started {second.launched!r}"
    )
    assert budgets == (3, 8), "headroom gates starts without decreasing either AIMD budget"
    assert scenario.sensor.memory_reads, "admission must consume public capacity.memory_bytes()"


def test_memory_threshold_headroom_is_rechecked_after_each_launch(memory_machine):
    scenario = memory_machine
    seed_budgets(scenario)
    scenario.sensor.memory_total = 20 * GB
    scenario.sensor.memory_free = 8 * GB
    budgets = tick_budgets(scenario)
    first = scenario.cases[0]
    # Check existing running/reserved before each start: residual free bytes are
    # 8, 6.5, 5, then 3.5 GB, so the fourth candidate must remain ready.
    assert len(first.launched) == 3, (
        f"one tick must stop admitting when its new runs consume the headroom reserve: {first.launched!r}"
    )
    assert len(first.store.active_runs()) == 3
    assert any("ready" in first.tracker.get_issue(number).labels for number in range(1, 9))
    assert budgets == (3, 8), "low headroom must not halve/decrement the learned budgets"


def test_memory_threshold_headroom_reopens_when_free_memory_recovers(memory_machine):
    scenario = memory_machine
    first, second = scenario.cases
    seed_budgets(scenario)
    occupy_machine_slots(first, (1, 2), pid=os.getpid())
    scenario.sensor.memory_total = 20 * GB
    scenario.sensor.memory_free = 8 * GB - 1
    budgets = [tick_budgets(scenario, index=1)]
    actual = [len(second.launched)]
    scenario.sensor.memory_free = 20 * GB
    budgets.append(tick_budgets(scenario, index=1))
    actual.append(len(second.launched))
    assert actual == [0, 4], f"ready work must resume on recovered headroom without manual reset: {actual!r}"
    assert budgets == [(3, 8), (3, 8)], "headroom recovery must not need an AIMD budget recovery"


def test_memory_threshold_budget_history_records_headroom_refusal_and_window_counts(memory_machine):
    scenario = memory_machine
    first, _ = scenario.cases
    seed_budgets(scenario)
    occupy_machine_slots(first, (1, 2), pid=os.getpid())
    scenario.sensor.memory_total = 20 * GB
    scenario.sensor.memory_free = 8 * GB - 1
    tick_budgets(scenario, index=1)
    path = scenario.root / "budget-history.jsonl"
    assert path.is_file(), "headroom admission refusal must append budget-history.jsonl"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "headroom history must append complete JSON lines"
    rows = [json.loads(line) for line in text.splitlines()]
    refusals = [row for row in rows if row.get("rule") == "headroom"]
    assert refusals, f"budget history must identify the headroom admission rule: {rows!r}"
    for row in refusals:
        assert (row.get("swap_windows"), row.get("pressure_windows")) == (0, 0), (
            f"headroom refusal with normal kernel and zero swap-ins must record zero streaks: {row!r}"
        )

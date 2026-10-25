"""Acceptance test for memory-critical-line. Locked by Foragent; do not edit."""

# Imported fixtures are registered by pytest and requested by parameter name.
# ruff: noqa: F401, F811

from dataclasses import asdict
import json

import pytest

from test_adaptive_budgets import sample_clock, sensor
from test_capacity_aimd import occupy_machine_slots
from test_machine_budget_config import (
    budget_repositories,
    machine_config,
    machine_environment,
    seed_budgets,
    tick_budgets,
)
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


@pytest.fixture
def critical_machine(budget_repositories, machine_environment, monkeypatch):
    machine_config(machine_environment, test_slots=3, max_parallel=8)
    scenario = budget_repositories({"max_parallel": 4, "test_slots": 3},
                                   {"max_parallel": 4, "test_slots": 3})
    # Neutral CPU load isolates memory from both CPU backoff and additive growth.
    scenario.sensor.load = 1.1

    def forbidden(pid):
        pytest.fail(f"memory pressure must never stop or signal a running worker: {pid}")

    for case in scenario.cases:
        monkeypatch.setattr(case.tick_ctx, "kill_runner", forbidden)
    seed_budgets(scenario, slots=3, parallel=8)
    return scenario


def test_memory_critical_line_warn_and_many_swapin_windows_leave_budgets_unchanged(
    critical_machine, monkeypatch,
):
    from foragent import capacity

    scenario = critical_machine
    scenario.sensor.kernel = "warn"
    # Even zero free bytes must not introduce a reservation or free-% admission rule.
    monkeypatch.setattr(capacity, "memory_bytes", lambda: (64_000_000_000, 0), raising=False)
    actual = [tick_budgets(scenario, 0, swapins=900) for _ in range(12)]
    assert actual == [(3, 8)] * 12, (
        f"warn, compression and any number of swap-in windows must not move budgets: {actual!r}"
    )
    assert len(scenario.cases[0].launched) == 4, "warn must admit work up to the current ceilings"
    path = scenario.root / "budget-history.jsonl"
    assert not path.exists() or path.read_text(encoding="utf-8") == "", (
        "memory facts that neither change CPU budgets nor hold starts must append no history"
    )


def test_memory_critical_line_holds_new_runs_without_touching_or_signalling_running_work(critical_machine):
    scenario = critical_machine
    for index, case in enumerate(scenario.cases, 1):
        occupy_machine_slots(case, (1,), pid=880000 + index)
    before = [[asdict(run) for run in case.store.active_runs()] for case in scenario.cases]
    scenario.sensor.kernel = "critical"
    budgets = tick_budgets(scenario, 1, seconds=0)
    # Compare the original records, including status, pid, timestamps and phase.
    after = [[asdict(case.store.get_run(run["number"])) for run in runs]
             for case, runs in zip(scenario.cases, before)]
    assert after == before, "critical must leave every running record untouched"
    assert all(case.launched == [] for case in scenario.cases), (
        "critical must hold all new runs even when the worker budget has free capacity"
    )
    assert budgets == (3, 8), "a critical hold must not change either budget"
    assert all(case.store.run_starts() == {} for case in scenario.cases), (
        "critical must not reserve new starts"
    )


def test_memory_critical_line_claims_no_new_test_slot_until_pressure_clears(critical_machine, monkeypatch):
    from foragent import capacity

    scenario = critical_machine
    waits = []

    def release_pressure():
        waits.append(scenario.sensor.kernel)
        scenario.sensor.kernel = "normal"

    # No command, thread or real sleep is needed to clear the fake pressure.
    monkeypatch.setattr(capacity.time, "sleep", lambda seconds: None)
    with capacity.hold_test_slot(3):
        scenario.sensor.kernel = "critical"
        with capacity.hold_test_slot(3, waiting=release_pressure):
            assert waits == ["critical"], "a free test slot must remain unclaimed while the kernel is critical"
            assert scenario.sensor.kernel == "normal"
        # The pre-existing holder survives the hold and the newly admitted command.
        assert capacity.effective_test_slots(3) == 3, "critical must not shrink the test-slot budget"


def test_memory_critical_line_leaving_critical_resumes_without_reset(critical_machine):
    from foragent import capacity

    scenario = critical_machine
    first, _ = scenario.cases
    seed_budgets(scenario, slots=3, parallel=3)
    scenario.sensor.kernel = "critical"
    held = tick_budgets(scenario, 0, seconds=0)
    launched = [len(first.launched)]
    scenario.sensor.kernel = "warn"  # Clearing to warn is sufficient, even with more swap-ins.
    resumed = tick_budgets(scenario, 0, seconds=0, swapins=900)
    launched.append(len(first.launched))
    assert launched == [0, 3], f"leaving critical must immediately admit up to the current budget: {launched!r}"
    assert held == resumed == (3, 3), "resuming must require no budget recovery or state reset"
    assert capacity.read_state(first.store)["parallel"] == 3


def test_memory_critical_line_floor_keys_are_accepted_and_ignored_in_both_files(
    budget_repositories, machine_environment,
):
    from foragent import machine_budget

    machine_config(machine_environment, test_slots=3, max_parallel=8,
                   test_slots_floor=3, parallel_floor=6)
    scenario = budget_repositories({"test_slots_floor": 3, "parallel_floor": 4})
    scenario.sensor.load = 1.1
    seed_budgets(scenario, slots=1, parallel=2)
    report = machine_budget.read().report()
    assert not {"test_slots_floor", "parallel_floor"}.intersection(report.get("unknown_keys", [])), (
        "legacy machine floor keys must be accepted without an unknown-key diagnostic"
    )
    assert tick_budgets(scenario, 0) == (1, 2), (
        "accepted repository and machine floor keys must never raise or clamp the current budgets"
    )
    seed_budgets(scenario, slots=3, parallel=8)
    scenario.sensor.load = 2.0
    assert tick_budgets(scenario, 0) == (2, 4), "legacy floors must not clamp CPU-driven decreases"


def test_memory_critical_line_each_held_tick_appends_one_history_row_with_kernel_level(critical_machine):
    scenario = critical_machine
    scenario.sensor.kernel = "critical"
    path = scenario.root / "budget-history.jsonl"
    for index in (0, 1):
        before = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        tick_budgets(scenario, index, seconds=0)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        appended = rows[len(before):]
        assert len(appended) == 1 and appended[0].get("rule") == "memory-critical", (
            f"each critical-held tick must append exactly one memory-critical row: {appended!r}"
        )
        assert appended[0].get("pressure") == "critical", (
            f"the critical hold must record its kernel level: {appended[0]!r}"
        )
        assert path.read_bytes().endswith(b"\n"), "history must append complete JSON lines"

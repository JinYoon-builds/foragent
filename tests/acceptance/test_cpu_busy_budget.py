"""Acceptance test for cpu-busy-budget. Locked by Foragent; do not edit."""

from collections import namedtuple
from datetime import timedelta
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

from test_adaptive_budgets import (
    adaptive_environment, history, machine_yml, offline_acceptance,
)
from test_capacity_aimd import isolated_home, machine_repositories


@pytest.fixture
def cpu_budget_tick(machine_repositories, adaptive_environment, monkeypatch):
    """Exercise real tick budgets with ready work, fake sensors and no worker launches."""
    from foragent import capacity, scheduler

    case, _ = machine_repositories
    case.tick_ctx.start_runs = False
    case.tick_ctx.baseline_launcher = lambda *args, **kwargs: None
    # capacity-aimd supplies a constant sample; restore the real sensor pipeline.
    monkeypatch.setattr(scheduler, "machine_load", capacity.machine_load)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "swapins_total", lambda: 100)
    machine_yml(slots=1, max_parallel=8)

    def tick(*, current, raw_load, busy):
        monkeypatch.setattr(os, "cpu_count", lambda: 4)
        monkeypatch.setattr(os, "getloadavg", lambda: (raw_load * 4, 0.0, 0.0))
        # The proposed public seam is absent today. Supply it so failures expose
        # the unchanged budget calculation rather than a monkeypatch error.
        monkeypatch.setattr(capacity, "cpu_busy", lambda: busy, raising=False)
        capacity.write_state(
            case.store, current,
            case.clock[0] - capacity.INCREASE_INTERVAL - timedelta(seconds=1),
        )
        (adaptive_environment / "test-slots.json").write_text(json.dumps({
            "slots": 1, "changed_at": case.clock[0].isoformat(),
        }), encoding="utf-8")

        report = scheduler.tick(case.tick_ctx)
        assert report.skipped is None, report
        assert case.launched == [], "measuring a budget must not launch workers in this test"
        state = capacity.read_state(case.store)
        assert state is not None, "the tick must persist its machine parallel budget"
        assert capacity.load_per_cpu() == pytest.approx(raw_load), (
            "load_per_cpu() must retain its raw one-minute-load meaning"
        )
        return SimpleNamespace(
            parallel=state["parallel"], sample=case.tick_ctx._budget_sample,
            root=adaptive_environment,
        )

    return tick


def assert_parallel_history(result, *, current, expected, signal, raw_load, busy):
    rows = [row for row in history(result.root) if row["budget"] == "parallel"]
    assert len(rows) == 1, f"one parallel transition must be recorded: {rows!r}"
    row = rows[0]
    assert (row["from"], row["to"]) == (current, expected), row
    assert row["load_per_cpu"] == pytest.approx(signal), row
    if busy is None:
        # Without a busy measurement the row stays exactly today's shape.
        assert not {"load_average_per_cpu", "cpu_busy"} & row.keys(), (
            f"an unknown busy fraction must not add fields to today's history row: {row!r}"
        )
        return
    assert {"load_average_per_cpu", "cpu_busy"} <= row.keys(), (
        f"history must distinguish the budget signal, raw load average and CPU busy fraction: {row!r}"
    )
    assert row["load_average_per_cpu"] == pytest.approx(raw_load), row
    assert row["cpu_busy"] == pytest.approx(busy), row


def test_cpu_busy_budget_io_wait_load_keeps_parallel(cpu_budget_tick):
    result = cpu_budget_tick(current=6, raw_load=1.82, busy=0.67)

    assert result.parallel == 6, (
        "1.82 load/CPU with only 67% busy must keep parallel at 6; "
        f"the busy-capped signal is between headroom and overload, got {result.parallel}"
    )
    assert result.sample == ("normal", pytest.approx(0.67 / 0.6))


def test_cpu_busy_budget_idle_headroom_grows_parallel_and_records_signals(cpu_budget_tick):
    result = cpu_budget_tick(current=3, raw_load=1.3, busy=0.5)

    assert result.parallel == 4, (
        "50% busy must permit additive growth after the increase interval despite "
        f"a raw load of 1.3/CPU; expected 3 -> 4, got {result.parallel}"
    )
    assert result.sample == ("normal", pytest.approx(0.5 / 0.6))
    assert_parallel_history(
        result, current=3, expected=4, signal=0.5 / 0.6, raw_load=1.3, busy=0.5,
    )


def test_cpu_busy_budget_true_overload_halves_parallel(cpu_budget_tick):
    result = cpu_budget_tick(current=6, raw_load=1.82, busy=0.95)

    assert result.parallel == 3, "95% busy must still halve an overloaded parallel budget"
    assert result.sample == ("normal", pytest.approx(0.95 / 0.6)), (
        "even when both signals exceed 1.5, machine_load must report the busy-capped signal"
    )
    assert_parallel_history(
        result, current=6, expected=3, signal=0.95 / 0.6, raw_load=1.82, busy=0.95,
    )


def test_cpu_busy_budget_unknown_busy_falls_back_to_load_average(cpu_budget_tick):
    result = cpu_budget_tick(current=6, raw_load=1.82, busy=None)

    assert result.parallel == 3, "an unknown busy fraction must preserve load-only halving"
    assert result.sample == ("normal", pytest.approx(1.82))
    # This edge already halves today and keeps today's history row unchanged.
    assert_parallel_history(
        result, current=6, expected=3, signal=1.82, raw_load=1.82, busy=None,
    )


def test_cpu_busy_budget_sensor_persists_deltas_and_rebases_resets(
    adaptive_environment, monkeypatch,
):
    import psutil

    from foragent import capacity

    busy_sensor = getattr(capacity, "cpu_busy", None)
    assert callable(busy_sensor), "capacity.cpu_busy() must expose cumulative CPU busy-time deltas"
    cpu_times = namedtuple("CPUTimes", "user nice system idle")
    samples = iter([
        cpu_times(20, 0, 10, 70),    # First total=100, idle=70: no previous window.
        cpu_times(60, 0, 40, 100),   # Delta total=100, idle=30: 70% busy.
        cpu_times(2, 0, 1, 7),      # Counters went backwards: establish a new baseline.
        cpu_times(42, 0, 31, 37),   # Delta total=100, idle=30 after the reset.
        OSError("CPU counters unavailable"),
    ])

    def read_cpu_times():
        sample = next(samples)
        if isinstance(sample, OSError):
            raise sample
        return sample

    monkeypatch.setattr(psutil, "cpu_times", read_cpu_times)
    adaptive_environment.mkdir(parents=True, exist_ok=True)
    sensor_file = adaptive_environment / capacity.SENSOR_FILE
    sensor_file.write_text(json.dumps({"swapins_total": 100}), encoding="utf-8")

    assert busy_sensor() is None, "the first CPU sample has no delta"
    saved = json.loads(sensor_file.read_text(encoding="utf-8"))
    assert saved["swapins_total"] == 100, "CPU sampling must preserve the swap-in baseline"
    assert saved.keys() - {"swapins_total"}, "CPU totals must persist beside swapins_total"

    def fresh_busy():
        # A fresh module has no in-memory counter cache: only the machine sensor
        # file can supply its previous window. Do not reload the shared facade.
        spec = importlib.util.spec_from_file_location("foragent._cpu_busy_budget_capacity", capacity.__file__)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "machine_root", lambda: adaptive_environment)
        return module.cpu_busy()

    assert fresh_busy() == pytest.approx(0.7), "30 idle ticks of 100 must yield 70% busy"
    assert fresh_busy() is None, "backwards CPU totals must discard the old window"
    assert fresh_busy() == pytest.approx(0.7), "sampling must recover from the reset baseline"
    assert fresh_busy() is None, "a CPU-counter read error must return None"
    assert json.loads(sensor_file.read_text(encoding="utf-8"))["swapins_total"] == 100

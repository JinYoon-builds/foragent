"""Acceptance test for resource-facts. Locked by Foragent; do not edit."""

from collections import namedtuple
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from test_orch_facts import NOW, facts, facts_case, isolated_home, no_facts_processes
from test_state_split import offline_acceptance


CPU = namedtuple("CPU", "user nice system idle")
DISK = namedtuple("DISK", "read_count write_count read_bytes write_bytes read_time write_time")
DISK_RATES = ("read_bytes_per_s", "write_bytes_per_s", "read_ops_per_s", "write_ops_per_s")
SAMPLE_FIELDS = {"at", "cpu_busy", "load_average_per_cpu", "memory_pressure", "test_slot_queue", *DISK_RATES}


@pytest.fixture
def resource_case(tmp_path, monkeypatch, isolated_home, offline_acceptance, no_facts_processes):
    """Use #333's CLI/store/clock and real tick, replacing only external inputs."""
    from foragent import capacity, cli, scheduler
    from foragent.config import load_config
    from foragent.tracker import FakeTracker

    case = facts_case(tmp_path, monkeypatch)
    # The machine record belongs beside the sensor, independently of this
    # fixture's XDG_STATE_HOME repository store and any chosen sample filename.
    monkeypatch.setattr(capacity, "machine_root", lambda: Path.home() / ".local/share/foragent")
    monkeypatch.delenv("FORAGENT_CPU_BUSY", raising=False)
    case.cpu = CPU(100, 0, 50, 250)
    case.disk = DISK(1000, 2000, 100_000, 200_000, 0, 0)
    case.load = 2.0
    case.pressure = "normal"
    case.reads = []

    def cpu_times(*args, **kwargs):
        case.reads.append("cpu")
        return case.cpu

    def disk_counters(*args, **kwargs):
        case.reads.append("disk")
        return case.disk

    monkeypatch.setattr(psutil, "cpu_times", cpu_times)
    monkeypatch.setattr(psutil, "disk_io_counters", disk_counters)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(os, "getloadavg", lambda: (case.load,) * 3)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: case.pressure)
    monkeypatch.setattr(capacity, "memory_pressure", lambda: case.pressure)
    monkeypatch.setattr(capacity, "swapins_total", lambda: 1000)
    monkeypatch.setattr(capacity, "datetime", cli.datetime)
    monkeypatch.setattr(capacity.time, "time", lambda: case.clock[0].timestamp())
    monkeypatch.setattr(capacity.time, "monotonic", lambda: 1000 + (case.clock[0] - NOW).total_seconds())

    case.ctx = scheduler.TickContext(
        repo="owner/repo", repo_root=case.root, store=case.store,
        tracker=FakeTracker(now=lambda: case.clock[0], strict=True), config=load_config(case.root),
        git=SimpleNamespace(fetch=lambda *args: None), now=lambda: case.clock[0],
        launcher=lambda number: pytest.fail("A resource tick must not launch a runner"),
        start_runs=False, runner_process_exists=lambda number: True,
        kill_runner=lambda pid: pytest.fail("A resource tick must not signal a runner"),
        baseline_launcher=lambda *args: pytest.fail("An empty repository has no baseline to build"),
    )
    return case


def gate_tick(case):
    from foragent.scheduler import tick

    report = tick(case.ctx)
    assert report.skipped is None, report
    assert case.gh_calls == [], "Even fixture ticks must remain offline"


def stored_sample(case):
    """Inspect the producer's persisted public fields without fixing its filename.

    A latest JSON snapshot or JSONL journal is acceptable. Budget-change rows
    and the existing CPU/swap baseline alone are not full resource samples.
    """
    from foragent import capacity

    root = capacity.machine_root()
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name == capacity.SENSOR_FILE:
            continue
        if path.suffix == ".json":
            rows = [json.loads(path.read_text(encoding="utf-8"))]
        elif path.suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            continue
        for row in reversed(rows):
            if isinstance(row, dict) and SAMPLE_FIELDS <= row.keys() and row["at"] == case.clock[0].isoformat():
                return {key: row[key] for key in SAMPLE_FIELDS}
    pytest.fail("Every gate tick must persist a complete machine resource sample beside the existing sensor file")


def resource_facts(case, capsys, monkeypatch, *, human=False):
    """Keep #333's zero-gh and unchanged-file assertions; disallow live sensors too."""
    from foragent import capacity, scheduler

    def forbidden(*args, **kwargs):
        pytest.fail("Resource facts must read local records, never sample the current machine")

    with monkeypatch.context() as guard:
        for module, names in (
            (psutil, ("cpu_times", "disk_io_counters")),
            (os, ("getloadavg",)),
            (capacity, ("machine_load", "cpu_busy", "kernel_pressure", "memory_pressure", "swapins_total")),
            (scheduler, ("machine_load",)),
        ):
            for name in names:
                guard.setattr(module, name, forbidden)
        result = facts(case, capsys, human=human)
    if human:
        return result
    assert isinstance(result.get("resources"), dict), "Facts must expose the resources used by the orchestrator"
    return result["resources"]


def second_counters(case):
    case.clock[0] += timedelta(seconds=20)
    # 80 CPU-seconds across four CPUs: 40 busy, 40 idle -> 0.5 busy.
    case.cpu = CPU(124, 0, 66, 290)
    case.disk = DISK(1040, 2060, 120_480, 240_960, 0, 0)
    case.load = 3.0
    case.pressure = "warn"


def test_resource_facts_two_ticks_persist_cpu_load_pressure_and_disk_deltas(resource_case, monkeypatch, capsys):
    case = resource_case
    gate_tick(case)
    second_counters(case)
    gate_tick(case)
    latest = stored_sample(case)
    assert latest["cpu_busy"] == pytest.approx(0.5)
    assert 0 <= latest["cpu_busy"] <= 1
    assert (latest["load_average_per_cpu"], latest["memory_pressure"]) == (0.75, "warn")
    assert [latest[key] for key in DISK_RATES] == pytest.approx([1024, 2048, 2, 3]), (
        "Divide cumulative byte/operation deltas by the 20s between ticks, not by uptime or CPU time")
    assert case.reads.count("cpu") == case.reads.count("disk") == 2, "Take one counter sample per tick"

    case.clock[0] += timedelta(seconds=7)
    observed = resource_facts(case, capsys, monkeypatch)["machine"]
    assert {key: observed[key] for key in SAMPLE_FIELDS} == latest
    assert observed["age_seconds"] == pytest.approx(7), "Age belongs to the latest recorded sample"
    assert resource_facts(case, capsys, monkeypatch)["machine"] == observed


def test_resource_facts_stored_executor_limits_and_github_reading_are_local(resource_case, monkeypatch, capsys):
    case = resource_case
    until = NOW + timedelta(hours=1)
    case.store.pause_executor("claude", until)
    case.store.pause_executor("codex", NOW - timedelta(seconds=1))
    # Limits come from the stored pauses, not a guess from utilization reports.
    case.store.record_usage("claude", [{"name": "5h", "used_percent": 2, "resets_at": until.isoformat()}])
    case.store.record_usage("codex", [{"name": "5h", "used_percent": 100, "resets_at": until.isoformat()}])
    reading = {"remaining": 321, "seen_at": (NOW - timedelta(minutes=3)).isoformat()}
    # No remaining-count record exists today: specify the normalized local
    # snapshot independently of the separate gh rate_limit producer.
    case.store._write_json(case.store.root / "github-rate-limit.json", reading)
    gate_tick(case)

    observed = resource_facts(case, capsys, monkeypatch)
    assert observed["executors"]["claude"] == {"state": "limited", "until": until.isoformat()}
    assert observed["executors"]["codex"] == {"state": "ok", "until": None}
    assert observed["github_api_remaining"] == reading, "Retain the reading time, not the facts observation time"
    assert case.gh_calls == []

    case.clock[0] = until + timedelta(seconds=1)
    expired = resource_facts(case, capsys, monkeypatch)
    assert expired["executors"]["claude"] == {"state": "ok", "until": None}
    assert expired["github_api_remaining"] == reading
    assert case.gh_calls == [], "An old reading or expired pause must not trigger a live refresh"


def test_resource_facts_queue_counts_test_slot_waiters_not_quota_or_finished_runs(resource_case, monkeypatch, capsys):
    case = resource_case
    for number, phase in ((1, "test-slot-wait"), (2, "test-slot-wait"),
                          (3, "implementing"), (4, "checking"), (5, "test-slot-wait")):
        case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
        case.store.update_run(number, phase=phase, phase_started_at=NOW.isoformat(),
                              waiting_until=(NOW + timedelta(minutes=1)).isoformat())
    case.store.finish_run(5, outcome="merged")
    gate_tick(case)
    observed = resource_facts(case, capsys, monkeypatch)
    assert type(observed["test_slot_queue"]) is int and observed["test_slot_queue"] == 2
    assert observed["machine"]["test_slot_queue"] == 2
    assert stored_sample(case)["test_slot_queue"] == 2

    case.store.update_run(1, phase="checking", waiting_until=None)
    second_counters(case)
    gate_tick(case)
    assert resource_facts(case, capsys, monkeypatch)["test_slot_queue"] == 1
    assert stored_sample(case)["test_slot_queue"] == 1, "Persist the current queue, not cumulative waiters"


def test_resource_facts_first_sample_is_unknown_and_human_resources_is_one_line(resource_case, monkeypatch, capsys):
    case = resource_case
    gate_tick(case)
    observed = resource_facts(case, capsys, monkeypatch)
    machine = observed["machine"]
    assert machine["cpu_busy"] is None, "One cumulative CPU reading cannot measure utilization"
    assert all(machine[key] is None for key in DISK_RATES), "Unknown disk rates must be null, not fabricated zeroes"
    assert datetime.fromisoformat(machine["at"]) == NOW and machine["age_seconds"] == 0
    assert machine["load_average_per_cpu"] == 0.5 and machine["memory_pressure"] == "normal"
    assert observed["github_api_remaining"] is None, "No stored GitHub reading means unknown, without a request"
    assert observed["test_slot_queue"] == 0
    assert {key: machine[key] for key in SAMPLE_FIELDS} == stored_sample(case)

    output = resource_facts(case, capsys, monkeypatch, human=True)
    lines = [line for line in output.splitlines() if line.strip().lower().startswith("resources")]
    assert len(lines) == 1, f"Show exactly one human resources line: {output}"
    assert "unknown" in lines[0].lower(), "The first sample must not present unknown utilization as idle"


def test_resource_facts_counter_reset_discards_old_rates_then_rebases(resource_case, monkeypatch, capsys):
    case = resource_case
    gate_tick(case)
    second_counters(case)
    gate_tick(case)
    case.clock[0] += timedelta(seconds=20)
    case.cpu = CPU(10, 0, 5, 25)
    case.disk = DISK(10, 20, 1000, 2000, 0, 0)
    gate_tick(case)
    reset = stored_sample(case)
    assert reset["cpu_busy"] is None and all(reset[key] is None for key in DISK_RATES), (
        "A counter reset has unknown activity; never retain stale rates or emit negative/zero utilization")
    observed = resource_facts(case, capsys, monkeypatch)["machine"]
    assert observed["cpu_busy"] is None and all(observed[key] is None for key in DISK_RATES)

    case.clock[0] += timedelta(seconds=20)
    case.cpu = CPU(34, 0, 21, 65)
    case.disk = DISK(50, 80, 21_480, 42_960, 0, 0)
    gate_tick(case)
    recovered = resource_facts(case, capsys, monkeypatch)["machine"]
    assert recovered["cpu_busy"] == pytest.approx(0.5)
    assert [recovered[key] for key in DISK_RATES] == pytest.approx([1024, 2048, 2, 3])

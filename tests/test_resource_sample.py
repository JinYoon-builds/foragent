"""The gate's machine resource sample: the cross-store test-slot queue and the stored row."""

from datetime import UTC, datetime, timedelta
import json

from foragent import capacity, resource_sample
from foragent.state import StateStore

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def waiter(store, number, *, phase="test-slot-wait", until=NOW + timedelta(minutes=1)):
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.update_run(number, phase=phase, waiting_until=until.isoformat() if until else None)


def test_slot_queue_counts_live_waiters_in_every_store_under_the_machine_root(tmp_path, monkeypatch):
    machine = tmp_path / "machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    own = StateStore(tmp_path / "elsewhere" / "owner__repo", now=lambda: NOW)
    sibling = StateStore(machine / "owner__other", now=lambda: NOW)
    waiter(own, 1)
    waiter(own, 2, until=NOW - timedelta(seconds=1))  # Its waiter stopped refreshing: not queued.
    waiter(own, 3, phase="checking")
    waiter(sibling, 4)
    waiter(sibling, 5)
    sibling.finish_run(5, outcome="merged")
    assert resource_sample.slot_queue(own, NOW) == 2


def test_record_writes_one_latest_row_beside_the_sensor(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "owner__repo", now=lambda: NOW)
    waiter(store, 1)
    sample = capacity.LoadSample("warn", 0.4, None, load_average_per_cpu=0.9, cpu_busy=0.25)
    sample.disk_rates = {"read_bytes_per_s": 1.0, "write_bytes_per_s": 2.0, "read_ops_per_s": 3.0, "write_ops_per_s": 4.0}
    resource_sample.record(store, sample, NOW)
    resource_sample.record(store, sample, NOW + timedelta(seconds=5))
    row = json.loads((tmp_path / resource_sample.FILE).read_text())
    assert row == {"at": (NOW + timedelta(seconds=5)).isoformat(), "cpu_busy": 0.25, "load_average_per_cpu": 0.9,
                   "memory_pressure": "warn", **sample.disk_rates, "test_slot_queue": 1}
    assert resource_sample.read(tmp_path / resource_sample.FILE) == row
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []


def test_disk_counter_reset_is_unknown_even_when_psutil_would_normalize_it(monkeypatch):
    """psutil's default nowrap=True adds earlier in-process values onto a reset; read raw counters."""
    from collections import namedtuple

    import psutil

    Disk = namedtuple("Disk", "read_count write_count read_bytes write_bytes")
    raw = [Disk(1000, 2000, 100_000, 200_000), Disk(10, 20, 1000, 2000)]
    clock = [100.0]

    def counters(perdisk=False, nowrap=True):
        value = raw.pop(0)
        if nowrap and not raw:
            value = Disk(*(1000 + field for field in value))  # What psutil's wrap cache would report.
        return value

    monkeypatch.setattr(psutil, "disk_io_counters", counters)
    monkeypatch.setattr(capacity.time, "time", lambda: clock[0])
    sensor = {}
    assert set(capacity._disk_rates(sensor).values()) == {None}
    clock[0] += 20
    assert set(capacity._disk_rates(sensor).values()) == {None}, "A reset has unknown activity"
    assert sensor["disk"]["read_bytes"] == 1000, "Rebase on the raw counters after a reset"


def test_facts_queue_matches_the_sample_when_the_store_is_outside_the_machine_root(tmp_path, monkeypatch):
    from foragent import orch_facts, orch_facts_machine

    machine = tmp_path / "machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    own = StateStore(tmp_path / "elsewhere" / "owner__repo", now=lambda: NOW)
    waiter(own, 1)
    waiter(StateStore(machine / "owner__other", now=lambda: NOW), 2)
    waiter(StateStore(machine / "owner__repo", now=lambda: NOW), 3)  # Same name, another store.
    resource_sample.record(own, capacity.LoadSample("normal", 0.1, None, load_average_per_cpu=0.1), NOW)
    assert json.loads((machine / resource_sample.FILE).read_text())["test_slot_queue"] == 3
    snapshot, frozen = orch_facts._freeze(own, tmp_path / "frozen")
    running = snapshot.active_runs()
    assert orch_facts_machine._slot_queue(snapshot, frozen, NOW, running) == 3
    assert orch_facts_machine._slot_queue(own, {}, NOW, own.active_runs()) == 3, "The tick's live read agrees"

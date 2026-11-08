"""The gate's machine resource sample: the cross-store test-slot queue and the stored row."""

from datetime import UTC, datetime, timedelta
import json

from foragent import capacity, orch_facts, resource_sample
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
    row = json.loads(resource_sample.path_for(store).read_text())
    assert row == {"at": (NOW + timedelta(seconds=5)).isoformat(), "cpu_busy": 0.25, "load_average_per_cpu": 0.9,
                   "memory_pressure": "warn", **sample.disk_rates, "test_slot_queue": 1,
                   "processes": {}}
    assert resource_sample.read(resource_sample.path_for(store)) == row
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
    assert json.loads(resource_sample.path_for(own).read_text())["test_slot_queue"] == 3
    snapshot, frozen = orch_facts._freeze(own, tmp_path / "frozen")
    running = snapshot.active_runs()
    assert orch_facts_machine._slot_queue(snapshot, frozen, NOW, running) == 3
    assert orch_facts_machine._slot_queue(own, {}, NOW, own.active_runs()) == 3, "The tick's live read agrees"


def test_an_older_sample_written_after_a_newer_one_never_hides_it(tmp_path, monkeypatch):
    """Two repositories' ticks: the one measured first publishes last (delayed in slot_queue)."""
    from foragent import orch_facts_machine

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    newer, older = (StateStore(tmp_path / name, now=lambda: NOW) for name in ("owner__newer", "owner__older"))
    resource_sample.record(newer, capacity.LoadSample("warn", 0.9, None, load_average_per_cpu=0.9),
                           NOW + timedelta(seconds=20))
    resource_sample.record(older, capacity.LoadSample("normal", 0.1, None, load_average_per_cpu=0.1), NOW)
    stored, pressure_load = orch_facts_machine.freeze_sample(tmp_path / "frozen", orch_facts._copy)
    assert stored["at"] == (NOW + timedelta(seconds=20)).isoformat()
    assert pressure_load == ("warn", 0.9)
    assert orch_facts_machine.sample({"stored": stored}, NOW + timedelta(seconds=27))["age_seconds"] == 7


def test_tick_publishes_the_time_measured_before_the_sensors(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler, scheduler_start

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    clock = [NOW]

    def slow_sensor():
        clock[0] += timedelta(seconds=30)  # Readings take time; publication happens after them.
        return capacity.LoadSample("normal", 0.2, None, load_average_per_cpu=0.2)

    monkeypatch.setattr(scheduler, "machine_load", slow_sensor)
    monkeypatch.setattr(scheduler, "update_test_slots", lambda *args, **kwargs: None)
    store = StateStore(tmp_path / "owner__repo", now=lambda: clock[0])
    ctx = SimpleNamespace(store=store, now=lambda: clock[0])
    scheduler_start._apply_budgets(ctx, {}, workers=False)
    assert json.loads(resource_sample.path_for(store).read_text())["at"] == NOW.isoformat()


def test_an_older_github_reading_never_replaces_a_newer_one(tmp_path):
    store = StateStore(tmp_path, now=lambda: NOW)
    store.record_github_remaining(100, NOW + timedelta(seconds=5))
    store.record_github_remaining(900, NOW)  # A runner's probe that arrived earlier, saved later.
    assert json.loads((tmp_path / "github-rate-limit.json").read_text()) == {
        "remaining": 100, "seen_at": (NOW + timedelta(seconds=5)).isoformat()}
    store.record_github_remaining(50, NOW + timedelta(seconds=9))
    assert json.loads((tmp_path / "github-rate-limit.json").read_text())["remaining"] == 50


class _Process:
    def __init__(self, name, *, rss=0, cpu=0.0, error=None):
        self.info, self.rss, self.cpu, self.error, self.signals = {"name": name}, rss, cpu, error, []

    def cpu_percent(self, interval=None):
        if self.error:
            raise self.error
        return self.cpu

    def memory_info(self):
        if self.error:
            raise self.error
        return type("Memory", (), {"rss": self.rss})()

    def send_signal(self, sig):
        self.signals.append(sig)


def _table(monkeypatch, processes, platform="darwin"):
    import psutil
    import sys

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(processes))


def test_process_usage_keeps_bytes_and_percent_apart_and_names_why_it_is_unavailable(monkeypatch):
    import psutil

    fseventsd = _Process("fseventsd", rss=3 * 1024 * 1024, cpu=12.5)
    _table(monkeypatch, [_Process("launchd", rss=1, cpu=1.0), fseventsd])
    assert capacity.process_usage("fseventsd") == {"process": "fseventsd", "state": "observed", "reason": None,
                                                   "rss_bytes": 3 * 1024 * 1024, "cpu_percent": 12.5}
    assert fseventsd.signals == []
    # A measured idle process is a real zero, distinct from an unavailable reading.
    _table(monkeypatch, [_Process("fseventsd", rss=4096, cpu=0.0)])
    assert capacity.process_usage("fseventsd")["cpu_percent"] == 0.0
    for processes, platform, reason in [
        ([_Process("launchd")], "darwin", "absent"),
        ([_Process("fseventsd", error=psutil.AccessDenied(1))], "darwin", "inaccessible"),
        ([_Process("fseventsd", error=psutil.NoSuchProcess(1))], "darwin", "absent"),
        ([_Process("fseventsd", rss=1, cpu=1.0)], "linux", "unsupported"),
    ]:
        _table(monkeypatch, processes, platform)
        assert capacity.process_usage("fseventsd") == {"process": "fseventsd", "state": "unavailable",
                                                       "reason": reason, "rss_bytes": None, "cpu_percent": None}


def test_record_stores_process_readings_and_read_keeps_only_valid_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "owner__repo", now=lambda: NOW)
    sample = capacity.LoadSample("normal", 0.1, None, load_average_per_cpu=0.1, cpu_busy=0.1)
    observed = {"process": "fseventsd", "state": "observed", "reason": None, "rss_bytes": 2048, "cpu_percent": 5.0}
    sample.processes = {"fseventsd": observed}
    resource_sample.record(store, sample, NOW)
    path = resource_sample.path_for(store)
    assert resource_sample.read(path)["processes"] == {"fseventsd": observed}
    row = json.loads(path.read_text())
    row["processes"]["fseventsd"] = {**observed, "rss_bytes": None}  # Observed without a number: invalid.
    row["processes"]["other"] = {"process": "other", "state": "unavailable", "reason": "absent",
                                 "rss_bytes": 0, "cpu_percent": None}  # Unavailable with a zero: invalid.
    path.write_text(json.dumps(row))
    assert resource_sample.read(path)["processes"] == {}


def test_an_older_sample_without_process_readings_reads_as_unknown_not_zero(tmp_path, monkeypatch):
    from foragent import orch_facts_machine

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "owner__repo", now=lambda: NOW)
    legacy = {"at": NOW.isoformat(), "cpu_busy": 0.25, "load_average_per_cpu": 0.9, "memory_pressure": "warn",
              **dict.fromkeys(capacity.DISK_RATES.values(), 1.0), "test_slot_queue": 0}
    path = resource_sample.path_for(store)
    path.write_text(json.dumps(legacy))
    stored = resource_sample.read(path)
    assert stored["memory_pressure"] == "warn" and stored["processes"] == {}
    facts = orch_facts_machine.sample({"stored": stored}, NOW + timedelta(seconds=7))
    assert facts["age_seconds"] == 7
    assert facts["processes"]["fseventsd"]["state"] == "unknown"
    assert facts["processes"]["fseventsd"]["rss_bytes"] is None
    assert facts["processes"]["fseventsd"]["cpu_percent"] is None


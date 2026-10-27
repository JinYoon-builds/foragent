from collections import namedtuple
from datetime import UTC, datetime, timedelta
import ctypes
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import psutil

from foragent import capacity, scheduler
from foragent.config import Config
from foragent.state import StateStore


@pytest.fixture(autouse=True)
def no_inherited_test_slot(monkeypatch):
    """`fa test` marks its children as holding a slot; these tests take slots themselves."""
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)


NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def dead_pid():
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.wait(timeout=10)
    return child.pid


@pytest.mark.parametrize("load,increase_ok,expected", [(1.2, True, 4), (0.9, True, 5), (1.49, False, 4)])
def test_next_parallel_holds_between_headroom_and_overload(load, increase_ok, expected):
    assert capacity.next_parallel(4, pressure="normal", load_per_cpu=load, lanes_with_work=9,
                                  ceiling=None, increase_ok=increase_ok) == expected


def test_next_parallel_never_goes_below_one():
    assert capacity.next_parallel(3, pressure="normal", load_per_cpu=0.1, lanes_with_work=0,
                                  ceiling=None, increase_ok=True) == 1


@pytest.mark.parametrize("level,expected", [(1, "normal"), (2, "warn"), (4, "critical"),
                                            (None, "normal"), (7, "normal")])
def test_darwin_pressure_maps_sysctl_levels(monkeypatch, level, expected):
    monkeypatch.setattr(capacity.sys, "platform", "darwin")
    monkeypatch.setattr(capacity, "_darwin_pressure_level", lambda: level)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("memory pressure must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    assert capacity.memory_pressure() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the macOS kernel sysctl")
def test_darwin_pressure_level_reads_kernel_in_process(monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("memory pressure must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    assert capacity._darwin_pressure_level() in {*capacity._DARWIN_PRESSURE, None}


@pytest.mark.parametrize("result,expected", [(0, 2), (1, None)])
def test_darwin_pressure_reads_sysctl_value_and_handles_errors(monkeypatch, result, expected):
    def sysctl(name, output, size, new_value, new_size):
        assert name == b"kern.memorystatus_vm_pressure_level"
        assert size._obj.value == ctypes.sizeof(ctypes.c_int)
        output._obj.value = 2
        return result

    monkeypatch.setattr(capacity.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(sysctlbyname=sysctl))
    assert capacity._darwin_pressure_level() == expected


@pytest.mark.parametrize("text,expected", [
    ("some avg10=0.50 avg60=0.10 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", "normal"),
    ("some avg10=15.00 avg60=1 avg300=0 total=1\nfull avg10=2.00 avg60=0 avg300=0 total=0\n", "warn"),
    ("some avg10=20.00 avg60=1 avg300=0 total=1\nfull avg10=12.00 avg60=0 avg300=0 total=0\n", "critical"),
])
def test_linux_pressure_reads_proc(monkeypatch, tmp_path, text, expected):
    path = tmp_path / "memory"
    path.write_text(text)
    monkeypatch.setattr(capacity.sys, "platform", "linux")
    monkeypatch.setattr(capacity, "Path", lambda _: path)
    assert capacity.memory_pressure() == expected


def test_linux_pressure_without_proc_reads_normal(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity.sys, "platform", "linux")
    monkeypatch.setattr(capacity, "Path", lambda _: tmp_path / "missing")
    assert capacity.memory_pressure() == "normal"


def test_load_per_cpu_divides_by_cpu_count(monkeypatch):
    monkeypatch.setattr(capacity.os, "getloadavg", lambda: (6.0, 1.0, 1.0))
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 8)
    assert capacity.load_per_cpu() == 0.75


def start(store, number, pid):
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), pid=pid)


def test_machine_runs_counts_own_runs_and_live_sibling_runs(tmp_path):
    own, other = StateStore(tmp_path / "own"), StateStore(tmp_path / "other", now=lambda: NOW)
    start(own, 1, dead_pid())  # Recovery, not the budget, settles this store's own runs.
    start(other, 1, os.getpid())
    start(other, 2, dead_pid())
    start(other, 3, None)  # Recorded but not yet launched.
    start(other, 4, os.getpid())
    other.finish_run(4, "merged")
    (tmp_path / "unrelated").mkdir()
    (tmp_path / "broken" / "runs").mkdir(parents=True)
    (tmp_path / "broken" / "runs" / "notes.json").write_text("{}")
    assert capacity.machine_runs(own, NOW) == 3


def test_state_round_trip_and_unreadable_state(tmp_path):
    store = StateStore(tmp_path / "repo")
    assert capacity.read_state(store) is None
    capacity.write_state(store, 3, NOW)
    assert capacity.read_state(store) == {"parallel": 3, "changed_at": NOW.isoformat()}
    (tmp_path / capacity.STATE_FILE).write_text("{not json")
    assert capacity.read_state(store) is None


def budget_ctx(tmp_path, now, max_parallel=None):
    """max_parallel is this repository's own ceiling; the machine's comes from machine.yml (#365)."""
    return SimpleNamespace(store=StateStore(tmp_path / "repo"), now=lambda: now,
                           config=Config(max_parallel=max_parallel))


def write_machine_yml(**values):
    from foragent import machine_budget

    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}: {value}\n" for key, value in values.items()), encoding="utf-8")


def test_budget_starts_at_initial_value_and_spaces_increases(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    ready = {f"lane-{n}": [n] for n in range(6)}
    ctx = budget_ctx(tmp_path, NOW)
    assert scheduler._machine_budget(ctx, ready) == capacity.INITIAL_PARALLEL
    assert scheduler._machine_budget(ctx, ready) == capacity.INITIAL_PARALLEL
    ctx.now = lambda: NOW + capacity.INCREASE_INTERVAL
    assert scheduler._machine_budget(ctx, ready) == capacity.INITIAL_PARALLEL + 1
    assert scheduler._machine_budget(ctx, ready) == capacity.INITIAL_PARALLEL + 1
    assert capacity.read_state(ctx.store)["changed_at"] == (NOW + capacity.INCREASE_INTERVAL).isoformat()


def test_project_ceiling_limits_the_gate_but_not_the_shared_value(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    capacity.write_state(StateStore(tmp_path / "repo"), 5, NOW - timedelta(hours=1))
    ctx = budget_ctx(tmp_path, NOW, max_parallel=2)
    budget = scheduler._machine_budget(ctx, {f"lane-{n}": [n] for n in range(8)})
    assert budget == 6 and capacity.read_state(ctx.store)["parallel"] == 6
    other = StateStore(tmp_path / "other")
    for number in (1, 2, 3):
        start(other, number, os.getpid())
    assert scheduler._has_slot(ctx, budget)  # Runs elsewhere do not count against the ceiling.
    for number in (1, 2):
        start(ctx.store, number, os.getpid())
    assert not scheduler._has_slot(ctx, budget)


def test_budget_demand_excludes_busy_and_paused_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    seen = {}

    def reducer(current, **inputs):
        seen.update(inputs)
        return current

    monkeypatch.setattr(scheduler, "next_parallel", reducer)
    ctx = budget_ctx(tmp_path, NOW)
    capacity.write_state(ctx.store, 5, NOW)
    ctx.store.start_run(1, branch="b", worktree=ctx.store.worktree_path(1), lane="busy", pid=os.getpid())
    ctx.store.pause_lane("paused", NOW + timedelta(hours=1))
    # One live run plus every ready issue in unpaused lanes (busy lanes included) caps this tick at 3.
    assert scheduler._machine_budget(ctx, {"busy": [2], "paused": [3], "free": [4]}) == 3
    assert seen["ceiling"] is None and seen["increase_ok"] is False


def test_machine_runs_counts_a_pidless_sibling_run_only_while_it_may_still_launch(tmp_path):
    own = StateStore(tmp_path / "own")
    for number, age in [(1, timedelta(minutes=1)), (2, timedelta(minutes=3))]:
        other = StateStore(tmp_path / "other", now=lambda age=age: NOW - age)
        other.start_run(number, branch=f"b{number}", worktree=other.worktree_path(number), pid=None)
    assert capacity.machine_runs(own, NOW) == 1


def test_machine_runs_counts_fresh_start_reservations_once(tmp_path):
    own = StateStore(tmp_path / "own", now=lambda: NOW)
    own.prepare_run_start(1)
    own.prepare_run_start(2)
    start(own, 2, os.getpid())  # A reservation that became a run counts once.
    stale = StateStore(tmp_path / "other", now=lambda: NOW - timedelta(minutes=3))
    stale.prepare_run_start(5)
    fresh = StateStore(tmp_path / "third", now=lambda: NOW)
    fresh.prepare_run_start(6)
    assert capacity.machine_runs(own, NOW) == 3


def test_lanes_cap_limits_the_tick_but_not_the_shared_value(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    changed = NOW - timedelta(seconds=30)
    capacity.write_state(StateStore(tmp_path / "repo"), 5, changed)
    ctx = budget_ctx(tmp_path, NOW)
    assert scheduler._machine_budget(ctx, {"a": [1], "b": [2]}) == 2
    assert capacity.read_state(ctx.store) == {"parallel": 5, "changed_at": changed.isoformat()}


@pytest.mark.parametrize("pressure", ["warn", "critical"])
def test_memory_pressure_never_moves_the_parallel_budget(tmp_path, pressure):
    # #497: warn, critical and swap-ins neither lower nor block growth; CPU load alone steps.
    ctx = budget_ctx(tmp_path, NOW)
    capacity.write_state(ctx.store, 3, NOW)
    ready = {f"lane-{n}": [n] for n in range(8)}
    assert scheduler._machine_budget(ctx, ready, capacity.LoadSample(pressure, 0.5, 900)) == 3
    assert not (tmp_path / capacity.HISTORY_FILE).exists()
    ctx.now = lambda: NOW + capacity.INCREASE_INTERVAL
    assert scheduler._machine_budget(ctx, ready, capacity.LoadSample(pressure, 0.5, 900)) == 4
    rows = [json.loads(line) for line in (tmp_path / capacity.HISTORY_FILE).read_text().splitlines()]
    assert [(row["from"], row["to"], row["pressure"]) for row in rows] == [(3, 4, pressure)]


def test_parallel_floor_in_machine_yml_is_ignored(tmp_path):
    write_machine_yml(parallel_floor=3, max_parallel=8)
    ctx = budget_ctx(tmp_path, NOW)
    capacity.write_state(ctx.store, 4, NOW - timedelta(hours=1))
    ready = {f"lane-{n}": [n] for n in range(8)}
    assert scheduler._machine_budget(ctx, ready, capacity.LoadSample("normal", 2.0, 0)) == 2
    assert capacity.read_state(ctx.store)["parallel"] == 2


@pytest.mark.parametrize("pressure,load,increase_ok,expected", [
    ("warn", 0.1, True, 1), ("normal", 1.5, True, 1), ("normal", 0.5, True, 3),
    ("normal", 0.5, False, 2), ("normal", 1.2, True, 2),
])
def test_next_test_slots_steps_by_one_within_ceiling(pressure, load, increase_ok, expected):
    assert capacity.next_test_slots(2, pressure=pressure, load_per_cpu=load, ceiling=3,
                                    increase_ok=increase_ok) == expected


def test_next_test_slots_stays_between_one_and_ceiling():
    assert capacity.next_test_slots(1, pressure="critical", load_per_cpu=3.0, ceiling=4, increase_ok=True) == 1
    assert capacity.next_test_slots(4, pressure="normal", load_per_cpu=0.0, ceiling=4, increase_ok=True) == 4
    assert capacity.next_test_slots(6, pressure="normal", load_per_cpu=1.2, ceiling=2, increase_ok=False) == 2


def test_effective_test_slots_reads_shared_value_capped_by_ceiling():
    write_machine_yml(test_slots=3)  # Above the default machine ceiling of 2 (#365).
    assert capacity.effective_test_slots(3) == 3
    capacity.update_test_slots(3, NOW, pressure="normal", load_per_cpu=2.0)
    assert capacity.effective_test_slots(3) == 2
    assert capacity.effective_test_slots(1) == 1
    (capacity.machine_root() / capacity.TEST_SLOTS_FILE).write_text("{not json")
    assert capacity.effective_test_slots(3) == 3


def test_update_test_slots_waits_between_increases():
    assert capacity.update_test_slots(3, NOW, pressure="normal", load_per_cpu=2.0) == 2
    assert capacity.update_test_slots(3, NOW + timedelta(minutes=1), pressure="normal", load_per_cpu=0.0) == 2
    assert capacity.update_test_slots(3, NOW + timedelta(minutes=2), pressure="normal", load_per_cpu=0.0) == 3


def test_test_slot_is_exclusive_up_to_the_effective_limit(monkeypatch):
    monkeypatch.setattr(capacity, "SLOT_POLL_SECONDS", 0.01)
    with capacity.hold_test_slot(2), capacity.hold_test_slot(2):
        assert capacity._take_test_slot(2) is None
    with capacity.hold_test_slot(1):
        assert capacity._take_test_slot(1) is None
    stream = capacity._take_test_slot(1)
    assert stream is not None
    stream.close()


def test_test_slot_held_above_a_lowered_limit_still_counts():
    write_machine_yml(test_slots=3)  # Above the default machine ceiling of 2 (#365).
    with capacity.hold_test_slot(3), capacity.hold_test_slot(3), capacity.hold_test_slot(3):
        # The AIMD value dropped to 2 while three commands ran; none may join.
        assert capacity._take_test_slot(2) is None
        assert capacity._take_test_slot(3) is None
        assert capacity._take_test_slot(4) is not None


def test_test_slot_waiter_reports_waiting_then_proceeds(monkeypatch):
    import threading

    monkeypatch.setattr(capacity, "SLOT_POLL_SECONDS", 0.01)
    reported, entered = threading.Event(), threading.Event()

    def waiter():
        with capacity.hold_test_slot(1, waiting=reported.set):
            entered.set()

    with capacity.hold_test_slot(1):
        thread = threading.Thread(target=waiter)
        thread.start()
        assert reported.wait(5)
        assert not entered.is_set()
    thread.join(5)
    assert entered.is_set()


def test_test_slot_of_a_killed_holder_is_released():
    directory = capacity.machine_root() / capacity.TEST_SLOTS_DIR
    directory.mkdir(parents=True)
    holder = subprocess.Popen([
        sys.executable, "-c",
        "import fcntl, os, sys\n"
        f"stream = open({str(directory / 'slot-0.lock')!r}, 'a')\n"
        "fcntl.flock(stream.fileno(), fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.readline()\n"
        "os._exit(9)\n",
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == "held\n"
        assert capacity._take_test_slot(1) is None
    finally:
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 9
        holder.stdin.close()
        holder.stdout.close()
    stream = capacity._take_test_slot(1)
    assert stream is not None
    stream.close()


def test_test_slot_held_by_the_parent_command_is_not_taken_twice(monkeypatch):
    monkeypatch.setenv(capacity.TEST_SLOT_ENV, "held")
    with capacity.hold_test_slot(1):
        # A Done-when command that runs `fa test` inherits the slot instead of waiting forever.
        with capacity.hold_test_slot(1):
            pass


def test_test_slot_stays_unclaimed_while_the_kernel_is_critical(monkeypatch):
    levels = ["critical", "critical", "normal"]
    reads, waits = [], []

    def kernel():
        reads.append(levels[0])
        return levels.pop(0) if len(levels) > 1 else levels[0]

    monkeypatch.setattr(capacity, "kernel_pressure", kernel)
    monkeypatch.setattr(capacity, "SLOT_POLL_SECONDS", 0)
    monkeypatch.setattr(capacity, "SLOT_HEARTBEAT_SECONDS", 0)
    with capacity.hold_test_slot(1, waiting=lambda: waits.append(reads[-1])):
        assert reads == ["critical", "critical", "normal"]  # Read again on every poll.
        assert waits == ["critical", "critical"]  # The free slot stayed unclaimed meanwhile.
        assert capacity._take_test_slot(1) is None  # Now this command holds the slot.


def test_test_slot_inherited_by_a_nested_command_ignores_critical(monkeypatch):
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "critical")
    monkeypatch.setenv(capacity.TEST_SLOT_ENV, "held")
    with capacity.hold_test_slot(1, waiting=lambda: pytest.fail("a held slot must not wait")):
        pass


@pytest.mark.parametrize("text,expected", [
    ("pgpgin 500\npswpin 7\npswpout 100\n", 7),
    ("pswpin 0\n", 0), ("pswpin -1\n", None),
    ("pswpin broken\n", None), ("pswpin\n", None), ("pgpgin 500\n", None),
])
def test_linux_swapins_reads_only_swapped_in_pages(tmp_path, monkeypatch, text, expected):
    path = tmp_path / "vmstat"
    path.write_text(text)
    monkeypatch.setattr(capacity.sys, "platform", "linux")
    monkeypatch.setattr(capacity, "Path", lambda _: path)
    assert capacity.swapins_total() == expected


def test_linux_swapins_unreadable_counter_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity.sys, "platform", "linux")
    monkeypatch.setattr(capacity, "Path", lambda _: tmp_path / "missing")
    assert capacity.swapins_total() is None


@pytest.mark.parametrize("result,expected", [(0, 2**40 + 7), (1, None)])
def test_darwin_swapins_uses_mach_64_bit_counter_and_releases_host_port(monkeypatch, result, expected):
    released = []

    def statistics(host, flavor, output, count):
        assert (host, flavor) == (123, 4)
        assert count._obj.value == 38
        output._obj.pageins = 999
        output._obj.swapins = 2**40 + 7
        return result

    libc = SimpleNamespace(
        mach_host_self=lambda: 123, host_statistics64=statistics,
        mach_port_deallocate=lambda task, host: released.append((task, host)),
    )
    class UInt32(ctypes.c_uint):
        @classmethod
        def in_dll(cls, *args):
            return SimpleNamespace(value=456)

    monkeypatch.setattr(capacity.sys, "platform", "darwin")
    monkeypatch.setattr(capacity.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(capacity.ctypes, "c_uint32", UInt32)
    assert capacity.swapins_total() == expected
    assert released == [(456, 123)]


def test_machine_load_rebases_after_unreadable_sample_and_corrupt_state(monkeypatch):
    total = [100]
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "warn")
    monkeypatch.setattr(capacity, "swapins_total", lambda: total[0])
    monkeypatch.setattr(capacity, "load_per_cpu", lambda: 0.5)
    assert capacity.machine_load().swapins is None
    total[0] = None
    assert capacity.machine_load() == ("warn", 0.5)
    total[0] = 200
    assert capacity.machine_load().swapins is None
    sample = capacity.machine_load()  # No swap-ins never relabels the kernel level (#497).
    assert sample == ("warn", 0.5) and sample.swapins == 0
    (capacity.machine_root() / capacity.SENSOR_FILE).write_text("{broken")
    assert capacity.machine_load() == ("warn", 0.5)


@pytest.mark.parametrize("field,expected", [("idle", 0.0), ("iowait", 0.0), ("steal", 1.0), ("system", 1.0)])
def test_cpu_busy_accounts_for_cpu_time_categories(monkeypatch, field, expected):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user nice system idle iowait steal guest guest_nice")
    initial = times(20, 0, 10, 70, 0, 0, 0, 0)
    samples = iter([initial, initial._replace(**{field: getattr(initial, field) + 100})])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    assert capacity.cpu_busy() is None
    assert capacity.cpu_busy() == pytest.approx(expected)


def test_cpu_busy_does_not_count_guest_time_twice(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user nice system idle iowait steal guest guest_nice")
    samples = iter([times(20, 0, 10, 70, 0, 0, 0, 0), times(60, 10, 10, 120, 0, 0, 20, 5)])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    assert capacity.cpu_busy() is None
    assert capacity.cpu_busy() == pytest.approx(0.5)


@pytest.mark.parametrize("next_times", [(20, 10, 70), (2, 1, 7), (100, 10, 60), (0, 10, 120)])
def test_cpu_busy_rebases_when_window_has_no_progress_or_counters_reset(monkeypatch, next_times):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user system idle")
    reset = times(*next_times)
    samples = iter([times(20, 10, 70), reset, times(reset.user + 70, reset.system, reset.idle + 30)])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    assert capacity.cpu_busy() is None
    assert capacity.cpu_busy() is None
    assert capacity.cpu_busy() == pytest.approx(0.7)


@pytest.mark.parametrize("busy", [0.0, 0.7, 1.0])
def test_cpu_busy_identical_counters_reuse_persisted_fraction(monkeypatch, busy):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user system idle")
    baseline = times(120, 10, 170)
    initial = times(baseline.user - 100 * busy, baseline.system, baseline.idle - 100 * (1 - busy))
    samples = iter([initial, baseline, baseline, baseline])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    assert capacity.cpu_busy() is None
    assert capacity.cpu_busy() == pytest.approx(busy)
    path = capacity.machine_root() / capacity.SENSOR_FILE
    saved = json.loads(path.read_text())
    assert saved["cpu_busy"] == pytest.approx(busy)
    assert capacity.cpu_busy() == pytest.approx(busy)
    assert capacity.cpu_busy() == pytest.approx(busy)
    assert json.loads(path.read_text()) == saved


@pytest.mark.parametrize("field", ["user", "idle"])
@pytest.mark.parametrize("has_fraction", [False, True])
@pytest.mark.parametrize("offset", [0, 9_000_000])
def test_cpu_busy_short_windows_accumulate_without_extreme_fractions(monkeypatch, field, has_fraction, offset):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user system idle")
    baseline = times(offset + 90, 10, offset + 100)
    initial = times(baseline.user - 70, baseline.system, baseline.idle - 30)
    samples = iter([
        *([initial] if has_fraction else []), baseline,
        baseline._replace(**{field: getattr(baseline, field) + 0.01}),
        times(baseline.user + 5, baseline.system, baseline.idle + 5),
        times(baseline.user + 10, baseline.system, baseline.idle + 10),
    ])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    if has_fraction:
        assert capacity.cpu_busy() is None
    expected = pytest.approx(0.7) if has_fraction else None
    assert capacity.cpu_busy() == expected
    path = capacity.machine_root() / capacity.SENSOR_FILE
    saved = json.loads(path.read_text())
    # One tick and then 10 CPU-seconds are both below the 20-second minimum.
    for _ in range(2):
        assert capacity.cpu_busy() == expected
        assert json.loads(path.read_text()) == saved
    assert capacity.cpu_busy() == pytest.approx(0.5)
    assert json.loads(path.read_text())["cpu_total"] == sum(baseline) + 20


@pytest.mark.parametrize("explicit_env", [False, True])
def test_cpu_busy_is_disabled_in_fresh_test_processes(explicit_env):
    script = """
import os
import psutil
from foragent import capacity
assert os.environ["FORAGENT_CPU_BUSY"] == "off"
def forbidden():
    raise AssertionError("disabled sensor must not read host CPU counters")
psutil.cpu_times = forbidden
assert capacity.cpu_busy() is None
"""
    subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True,
        env=dict(os.environ) if explicit_env else None,
    )


@pytest.mark.parametrize("error", [OSError, psutil.Error, NotImplementedError])
def test_cpu_busy_unavailable_counter_is_unknown(monkeypatch, error):
    def unavailable():
        raise error("CPU counters unavailable")

    monkeypatch.setattr(psutil, "cpu_times", unavailable)
    assert capacity.cpu_busy() is None


def test_machine_load_preserves_cpu_and_swapin_baselines_together(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    times = namedtuple("CPUTimes", "user system idle")
    samples, swapins = iter([times(20, 10, 70), times(60, 40, 100)]), iter([100, 125])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(samples))
    monkeypatch.setattr(capacity, "swapins_total", lambda: next(swapins))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "load_per_cpu", lambda: 2.0)
    first, second = capacity.machine_load(), capacity.machine_load()
    assert first == ("normal", 2.0) and first.swapins is None and first.cpu_busy is None
    assert second == ("normal", pytest.approx(0.7 / 0.6))
    assert second.swapins == 25 and second.load_average_per_cpu == 2.0
    assert second.cpu_busy == pytest.approx(0.7)
    saved = json.loads((capacity.machine_root() / capacity.SENSOR_FILE).read_text())
    assert saved["swapins_total"] == 125


@pytest.mark.parametrize("busy,expected", [(None, 0.4), (0.0, 0.0), (0.9, 0.4)])
def test_machine_load_keeps_raw_load_as_the_upper_bound(monkeypatch, busy, expected):
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "swapins_total", lambda: 100)
    monkeypatch.setattr(capacity, "load_per_cpu", lambda: 0.4)
    monkeypatch.setattr(capacity, "cpu_busy", lambda: busy)
    sample = capacity.machine_load()
    assert sample == ("normal", expected)
    assert sample.load_average_per_cpu == 0.4 and sample.cpu_busy == busy


@pytest.mark.parametrize("budget", ["parallel", "test_slots"])
@pytest.mark.parametrize("pressure,load", [("warn", 2.0), ("critical", 2.0), ("normal", 2.0)])
def test_budget_decreases_are_rate_limited_across_repositories(tmp_path, monkeypatch, budget, pressure, load):
    root = capacity.machine_root()
    root.mkdir(parents=True)
    contexts = [budget_ctx(root, NOW), budget_ctx(root, NOW)]
    contexts[1].store = StateStore(root / "other")
    if budget == "parallel":
        capacity.write_state(contexts[0].store, 8, NOW)
    else:
        (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps({"slots": 8, "changed_at": NOW.isoformat()}))
    monkeypatch.setattr(scheduler, "machine_load", lambda: capacity.LoadSample(pressure, load, 7))

    def update(now, context):
        if budget == "parallel":
            context.now = lambda: now
            return scheduler._machine_budget(context, {f"lane-{i}": [i] for i in range(8)})
        return capacity.update_test_slots(8, now, pressure=pressure, load_per_cpu=load, swapins=7)

    after = 4 if budget == "parallel" else 7
    assert update(NOW, contexts[0]) == after
    state_path = root / (capacity.STATE_FILE if budget == "parallel" else capacity.TEST_SLOTS_FILE)
    unchanged_state = state_path.read_bytes()
    unchanged_history = (root / capacity.HISTORY_FILE).read_bytes()
    for moment in (NOW, NOW + capacity.DECREASE_INTERVAL - timedelta(microseconds=1)):
        assert update(moment, contexts[1]) == after
        assert state_path.read_bytes() == unchanged_state
        assert (root / capacity.HISTORY_FILE).read_bytes() == unchanged_history
    assert update(NOW + capacity.DECREASE_INTERVAL, contexts[1]) == (2 if budget == "parallel" else 6)
    rows = [json.loads(line) for line in (root / capacity.HISTORY_FILE).read_text().splitlines()]
    assert len(rows) == 2
    assert [row["swapins"] for row in rows] == [7, 7]


def test_test_slots_decrease_immediately_after_growth_and_retain_decrease_clock():
    root = capacity.machine_root()
    root.mkdir(parents=True)
    (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps({
        "slots": 4, "changed_at": (NOW - capacity.INCREASE_INTERVAL).isoformat(),
    }))
    assert capacity.update_test_slots(8, NOW, pressure="normal", load_per_cpu=0.5) == 5
    assert capacity.update_test_slots(8, NOW + timedelta(seconds=1), pressure="normal", load_per_cpu=2.0) == 4
    state = capacity.read_test_slots()
    grown_at = NOW + capacity.INCREASE_INTERVAL + timedelta(seconds=1)
    assert capacity.update_test_slots(8, grown_at, pressure="normal", load_per_cpu=0.5) == 5
    assert capacity.read_test_slots()["decreased_at"] == state["decreased_at"]


@pytest.mark.parametrize("pressure", ["warn", "critical"])
def test_memory_pressure_never_moves_test_slots(pressure):
    # #497: memory is recorded as a fact only; growth follows CPU load alone.
    assert capacity.update_test_slots(3, NOW, pressure=pressure, load_per_cpu=0.5, swapins=60) == 3
    assert not (capacity.machine_root() / capacity.HISTORY_FILE).exists()
    root = capacity.machine_root()
    (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps({"slots": 2, "changed_at": NOW.isoformat()}))
    later = NOW + capacity.INCREASE_INTERVAL
    assert capacity.update_test_slots(3, later, pressure=pressure, load_per_cpu=0.5, swapins=60) == 3
    rows = [json.loads(line) for line in (root / capacity.HISTORY_FILE).read_text().splitlines()]
    assert rows == [{
        "at": later.isoformat(), "budget": "test_slots", "from": 2, "to": 3,
        "wanted": 3, "pressure": pressure, "load_per_cpu": 0.5, "swapins": 60,
    }]


def test_test_slots_floor_in_machine_yml_is_ignored():
    write_machine_yml(test_slots=3, test_slots_floor=3)
    assert capacity.update_test_slots(3, NOW, pressure="normal", load_per_cpu=2.0) == 2
    assert capacity.effective_test_slots(3) == 2


def test_machine_max_parallel_caps_the_shared_value_during_decrease_cooldown(tmp_path):
    write_machine_yml(max_parallel=2)
    ctx = budget_ctx(tmp_path, NOW)
    # A decrease just happened; the clamp to a lower ceiling does not wait for it.
    capacity.write_state(ctx.store, 8, NOW - timedelta(hours=1), decreased_at=NOW.isoformat())
    ready = {f"lane-{n}": [n] for n in range(8)}
    assert scheduler._machine_budget(ctx, ready, capacity.LoadSample("normal", 0.5, 0)) == 2
    state = capacity.read_state(ctx.store)
    assert state["parallel"] == 2
    assert state["decreased_at"] == NOW.isoformat()  # A clamp is not an AIMD decrease.
    rows = [json.loads(line) for line in (tmp_path / capacity.HISTORY_FILE).read_text().splitlines()]
    assert [(row["budget"], row["from"], row["to"]) for row in rows] == [("parallel", 8, 2)]
    # The clamp happened alone; AIMD resumes on the next tick within the ceiling.
    ctx.now = lambda: NOW + capacity.INCREASE_INTERVAL
    assert scheduler._machine_budget(ctx, ready, capacity.LoadSample("normal", 0.5, 0)) == 2

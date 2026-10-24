from datetime import UTC, datetime, timedelta
import ctypes
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

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
    return SimpleNamespace(store=StateStore(tmp_path / "repo"), now=lambda: now,
                           config=Config(max_parallel=max_parallel))


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
    # One live run plus one free ready lane caps this tick at 2.
    assert scheduler._machine_budget(ctx, {"busy": [2], "paused": [3], "free": [4]}) == 2
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
    assert capacity.effective_test_slots(3) == 3
    capacity.update_test_slots(3, NOW, pressure="warn", load_per_cpu=0.0)
    assert capacity.effective_test_slots(3) == 2
    assert capacity.effective_test_slots(1) == 1
    (capacity.machine_root() / capacity.TEST_SLOTS_FILE).write_text("{not json")
    assert capacity.effective_test_slots(3) == 3


def test_update_test_slots_waits_between_increases():
    assert capacity.update_test_slots(3, NOW, pressure="warn", load_per_cpu=0.0) == 2
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
        "import fcntl, sys, time\n"
        f"stream = open({str(directory / 'slot-0.lock')!r}, 'a')\n"
        "fcntl.flock(stream.fileno(), fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n",
    ], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == "held\n"
        assert capacity._take_test_slot(1) is None
    finally:
        holder.kill()
        holder.wait(timeout=10)
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
    assert capacity.machine_load() == ("normal", 0.5)
    (capacity.machine_root() / capacity.SENSOR_FILE).write_text("{broken")
    assert capacity.machine_load() == ("warn", 0.5)


@pytest.mark.parametrize("budget", ["parallel", "test_slots"])
@pytest.mark.parametrize("pressure,load", [("warn", 0.5), ("critical", 0.5), ("normal", 2.0)])
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
    assert capacity.update_test_slots(8, NOW + timedelta(seconds=1), pressure="warn", load_per_cpu=0.5) == 4
    state = capacity.read_test_slots()
    grown_at = NOW + capacity.INCREASE_INTERVAL + timedelta(seconds=1)
    assert capacity.update_test_slots(8, grown_at, pressure="normal", load_per_cpu=0.5) == 5
    assert capacity.read_test_slots()["decreased_at"] == state["decreased_at"]

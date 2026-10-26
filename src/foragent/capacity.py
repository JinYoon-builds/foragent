"""Machine-wide run budget and test slots: AIMD on CPU load (D60, #243).

Memory is a fact, not a budget input (#497): only the kernel's critical level
matters, and it holds new starts and new test slots without changing a budget.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import IO, Any

import psutil

from foragent.state import Run, StateStore

# Below this load per CPU the machine has headroom for one more run.
HEADROOM_LOAD = 1.0
# At or above this load per CPU runs slow each other down; halve at once.
OVERLOAD_LOAD = 1.5
# The budget before any measurement; swapping started at 3+ full test suites.
INITIAL_PARALLEL = 2
# A new run takes minutes to grow its memory; space additive increases.
INCREASE_INTERVAL = timedelta(minutes=2)
# Independent of increases: pressure after growth still decreases immediately.
DECREASE_INTERVAL = INCREASE_INTERVAL
# A run without a pid, or a reserved start, may still launch for this long.
LAUNCH_GRACE = timedelta(minutes=2)
STATE_FILE = "capacity.json"
LOCK_FILE = "capacity.lock"
TEST_SLOTS_FILE = "test-slots.json"
TEST_SLOTS_LOCK = "test-slots.lock"
TEST_SLOTS_DIR = "test-slots"
SENSOR_FILE = "memory-sample.json"
SENSOR_LOCK = "memory-sample.lock"
# Accumulate five wall-seconds × CPU count of CPU time before advancing the baseline.
CPU_BUSY_MIN_SECONDS = 5.0
HISTORY_FILE = "budget-history.jsonl"
# Set for a command that runs inside a held slot, so a nested `fa test` reuses it.
TEST_SLOT_ENV = "FORAGENT_TEST_SLOT"
HELD_ENV = {TEST_SLOT_ENV: "held"}
# How often a waiting test command re-checks for a free slot.
SLOT_POLL_SECONDS = 0.1
# How often a waiting test command reports that it is still waiting.
SLOT_HEARTBEAT_SECONDS = 10.0

_DARWIN_PRESSURE = {1: "normal", 2: "warn", 4: "critical"}


def next_parallel(
    current: int, *, pressure: str, load_per_cpu: float, lanes_with_work: int,
    ceiling: int | None, increase_ok: bool,
) -> int:
    """Halve on pressure or overload, add one with headroom, cap by work and ceiling."""
    if pressure != "normal" or load_per_cpu >= OVERLOAD_LOAD:
        value = current // 2
    elif increase_ok and load_per_cpu < HEADROOM_LOAD:
        value = current + 1
    else:
        value = current
    value = min(value, lanes_with_work)
    if ceiling is not None:
        value = min(value, ceiling)
    return max(1, value)


def _darwin_pressure_level() -> int | None:
    """Read kern.memorystatus_vm_pressure_level in-process; a tick starts no subprocess for it."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if libc.sysctlbyname(
            b"kern.memorystatus_vm_pressure_level", ctypes.byref(value), ctypes.byref(size),
            None, ctypes.c_size_t(0),
        ) != 0:
            return None
    except (OSError, AttributeError):
        return None
    return value.value


def memory_pressure() -> str:
    """Return normal, warn or critical; an unreadable sensor reads as normal."""
    if sys.platform == "darwin":
        return _DARWIN_PRESSURE.get(_darwin_pressure_level(), "normal")
    try:
        lines = Path("/proc/pressure/memory").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "normal"
    stalls = {}
    for line in lines:
        kind, *fields = line.split()
        values = dict(field.split("=", 1) for field in fields if "=" in field)
        try:
            stalls[kind] = float(values["avg10"])
        except (KeyError, ValueError):
            continue
    if stalls.get("full", 0) >= 10 or stalls.get("some", 0) >= 40:
        return "critical"
    if stalls.get("some", 0) >= 10:
        return "warn"
    return "normal"


def kernel_pressure() -> str:
    """The raw kernel pressure level: normal, warn or critical."""
    return memory_pressure()


class _VMStatistics64(ctypes.Structure):
    # mach/vm_statistics.h: natural_t is 32 bits, counters are 64 bits.
    _fields_ = (
        [(name, ctypes.c_uint32) for name in (
            "free_count", "active_count", "inactive_count", "wire_count",
        )]
        + [(name, ctypes.c_uint64) for name in (
            "zero_fill_count", "reactivations", "pageins", "pageouts", "faults",
            "cow_faults", "lookups", "hits", "purges",
        )]
        + [(name, ctypes.c_uint32) for name in ("purgeable_count", "speculative_count")]
        + [(name, ctypes.c_uint64) for name in (
            "decompressions", "compressions", "swapins", "swapouts",
        )]
        + [(name, ctypes.c_uint32) for name in (
            "compressor_page_count", "throttled_count", "external_page_count", "internal_page_count",
        )]
        + [("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]
    )


def swapins_total() -> int | None:
    """Cumulative swapped-in pages, read in-process from Mach or /proc/vmstat."""
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            statistics = _VMStatistics64()
            count = ctypes.c_uint32(ctypes.sizeof(statistics) // ctypes.sizeof(ctypes.c_int))
            host = libc.mach_host_self()
            try:
                if libc.host_statistics64(host, 4, ctypes.byref(statistics), ctypes.byref(count)) != 0:
                    return None
                return statistics.swapins
            finally:
                libc.mach_port_deallocate(ctypes.c_uint32.in_dll(libc, "mach_task_self_").value, host)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if fields[:1] == ["pswpin"]:
                total = int(fields[1])
                return total if total >= 0 else None
    except (OSError, ValueError, IndexError):
        pass
    return None


class LoadSample(tuple[str, float]):
    """Keep the public (pressure, load) pair and its sensor measurements together."""

    swapins: int | None
    load_average_per_cpu: float | None
    cpu_busy: float | None

    def __new__(
        cls, pressure: str, load: float, swapins: int | None, *,
        load_average_per_cpu: float | None = None, cpu_busy: float | None = None,
    ):
        sample = super().__new__(cls, (pressure, load))
        sample.swapins = swapins
        sample.load_average_per_cpu = load_average_per_cpu
        sample.cpu_busy = cpu_busy
        return sample


def load_per_cpu() -> float:
    """One-minute load average divided by the CPU count (vm.loadavg, /proc/loadavg)."""
    try:
        load = os.getloadavg()[0]
    except OSError:
        return 0.0
    return max(0.0, load) / (os.cpu_count() or 1)


def _read_sensor(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / SENSOR_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def cpu_busy() -> float | None:
    """Busy fraction since the last persisted CPU sample; iowait is idle, steal is busy."""
    if os.environ.get("FORAGENT_CPU_BUSY") == "off":
        return None
    try:
        root = machine_root()
        with _flock(root / SENSOR_LOCK):
            times = psutil.cpu_times()
            # Linux guest time is already included in user/nice counters.
            total = sum(times) - getattr(times, "guest", 0) - getattr(times, "guest_nice", 0)
            idle = times.idle + getattr(times, "iowait", 0)
            sensor = _read_sensor(root)
            previous_total, previous_idle = sensor.get("cpu_total"), sensor.get("cpu_idle")
            busy = None
            if (type(previous_total) in (int, float) and type(previous_idle) in (int, float)
                    and 0 <= previous_idle <= previous_total):
                elapsed, idle_elapsed = total - previous_total, idle - previous_idle
                # Subtracting float counters can put an all-idle delta just above total.
                if elapsed >= 0 and 0 <= idle_elapsed <= elapsed + 2 * math.ulp(total):
                    if elapsed < CPU_BUSY_MIN_SECONDS * (os.cpu_count() or 1):
                        busy = sensor.get("cpu_busy")
                        return busy if type(busy) in (int, float) and 0 <= busy <= 1 else None
                    busy = max(0.0, min(1.0, 1 - idle_elapsed / elapsed))
            # First samples and counter resets discard the old fraction and rebase.
            sensor.update(cpu_total=total, cpu_idle=idle, cpu_busy=busy)
            (root / SENSOR_FILE).write_text(json.dumps(sensor), encoding="utf-8")
        return busy
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError, psutil.Error):
        return None


def machine_load() -> LoadSample:
    root = machine_root()
    with _flock(root / SENSOR_LOCK):
        pressure, total = kernel_pressure(), swapins_total()
        sensor = _read_sensor(root)
        previous = sensor.get("swapins_total")
        if type(previous) is not int or previous < 0:
            previous = None
        # A reset (e.g. reboot) establishes a new baseline without negative activity.
        swapins = max(0, total - previous) if total is not None and previous is not None else None
        sensor["swapins_total"] = total
        (root / SENSOR_FILE).write_text(json.dumps(sensor), encoding="utf-8")
    # Swap-ins are recorded as a fact; they never reclassify the kernel level.
    raw_load, busy = load_per_cpu(), cpu_busy()
    load = min(raw_load, busy / 0.6) if busy is not None else raw_load
    return LoadSample(pressure, load, swapins, load_average_per_cpu=raw_load, cpu_busy=busy)


def _within_grace(timestamp: str | None, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(timestamp) < LAUNCH_GRACE
    except (TypeError, ValueError):
        return False


def _holds_slot(run: Run, now: datetime) -> bool:
    if run.pid is None:
        # Between its record and its launch; after the grace it is a leftover.
        return _within_grace(run.started_at, now)
    try:
        os.kill(run.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _store_slots(store: StateStore, now: datetime, *, own: bool) -> int:
    runs = store.active_runs()
    running = {run.number for run in runs}
    count = len(runs) if own else sum(1 for run in runs if _holds_slot(run, now))
    # A start reserves its slot before the GitHub labels and the run record.
    return count + sum(
        1 for number, prepared_at in store.run_starts().items()
        if number not in running and _within_grace(prepared_at, now)
    )


def machine_runs(store: StateStore, now: datetime) -> int:
    """Count this store's active runs and the live runs of every sibling store."""
    count = _store_slots(store, now, own=True)
    for root in store.root.parent.iterdir():
        if root == store.root or not ((root / "runs").is_dir() or (root / "run-starts").is_dir()):
            continue
        try:
            count += _store_slots(StateStore(root), now, own=False)
        except (OSError, ValueError, TypeError, KeyError):
            continue  # Not a readable Foragent store; it cannot hold a slot.
    return count


@contextmanager
def machine_lock(store: StateStore) -> Iterator[None]:
    """Serialize budget updates and run admission across every repository's gate."""
    with _flock(store.root.parent / LOCK_FILE):
        yield


def read_state(store: StateStore) -> dict[str, Any] | None:
    try:
        value = json.loads((store.root.parent / STATE_FILE).read_text(encoding="utf-8"))
        int(value["parallel"]), datetime.fromisoformat(value["changed_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return value


def write_state(
    store: StateStore, parallel: int, changed_at: datetime, *, decreased_at: str | None = None,
) -> None:
    value = {
        "parallel": parallel, "changed_at": changed_at.astimezone(UTC).isoformat(),
    }
    if decreased_at is not None:
        value["decreased_at"] = decreased_at
    store._write_json(store.root.parent / STATE_FILE, value)


def machine_root() -> Path:
    """The data root every repository's store lives under (StateStore.for_repo's parent)."""
    return Path.home() / ".local/share/foragent"


def next_test_slots(
    current: int, *, pressure: str, load_per_cpu: float, ceiling: int, increase_ok: bool,
) -> int:
    """Remove one slot on pressure or overload, add one with headroom, keep 1..ceiling."""
    if pressure != "normal" or load_per_cpu >= OVERLOAD_LOAD:
        value = current - 1
    elif increase_ok and load_per_cpu < HEADROOM_LOAD:
        value = current + 1
    else:
        value = current
    return max(1, min(value, ceiling))


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_test_slots() -> dict[str, Any] | None:
    try:
        value = json.loads((machine_root() / TEST_SLOTS_FILE).read_text(encoding="utf-8"))
        if type(value["slots"]) is not int or value["slots"] < 1:
            return None
        datetime.fromisoformat(value["changed_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return value


def effective_test_slots(ceiling: int) -> int:
    """The shared AIMD value, capped by machine.yml and this repository's lower ceiling."""
    from foragent import machine_budget

    ceiling = min(ceiling, machine_budget.values()["test_slots"])
    state = read_test_slots()
    return max(1, min(state["slots"], ceiling)) if state else max(1, ceiling)


def decrease_allowed(state: dict[str, Any] | None, now: datetime) -> bool:
    """Rate-limit decreases across ticks and repositories, separately from growth."""
    try:
        return now - datetime.fromisoformat(state["decreased_at"]) >= DECREASE_INTERVAL
    except (TypeError, ValueError, KeyError):
        return True


def append_memory_hold(
    root: Path, now: datetime, *, pressure: str, load_per_cpu: float, swapins: int | None,
    load_average_per_cpu: float | None = None, cpu_busy: float | None = None,
) -> None:
    """Record a tick whose new starts the critical kernel level held back."""
    row = {
        "at": now.astimezone(UTC).isoformat(), "rule": "memory-critical", "pressure": pressure,
        "load_per_cpu": load_per_cpu, "swapins": swapins,
    }
    if cpu_busy is not None:
        row.update(load_average_per_cpu=load_average_per_cpu, cpu_busy=cpu_busy)
    with _flock(root / "budget-history.lock"):
        with (root / HISTORY_FILE).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")


def append_budget_history(
    root: Path, now: datetime, budget: str, current: int, value: int, *,
    pressure: str, load_per_cpu: float, swapins: int | None, wanted: int | None = None,
    load_average_per_cpu: float | None = None, cpu_busy: float | None = None,
) -> None:
    if current == value and (wanted is None or wanted == value):
        return
    row = {
        "at": now.astimezone(UTC).isoformat(), "budget": budget,
        "from": current, "to": value, "pressure": pressure,
        "load_per_cpu": load_per_cpu, "swapins": swapins,
    }
    if cpu_busy is not None:
        row.update(load_average_per_cpu=load_average_per_cpu, cpu_busy=cpu_busy)
    if wanted is not None:
        row["wanted"] = wanted
    # Both budgets and all repository gates share one append lock.
    with _flock(root / "budget-history.lock"):
        with (root / HISTORY_FILE).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")


def _write_test_slots(root: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{TEST_SLOTS_FILE}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
    os.replace(temporary, root / TEST_SLOTS_FILE)


def update_test_slots(
    ceiling: int, now: datetime, *, pressure: str, load_per_cpu: float, swapins: int | None = None,
    load_average_per_cpu: float | None = None, cpu_busy: float | None = None,
) -> int:
    """Apply the guard and the CPU-load step; ``pressure`` is only recorded in history."""
    root = machine_root()
    with _flock(root / TEST_SLOTS_LOCK):
        state = read_test_slots()
        if state and state["slots"] > ceiling:
            # A lower ceiling applies at once and alone: no AIMD step, no decrease
            # interval, no decreased_at. Held slots finish; new holds see the ceiling.
            _write_test_slots(root, {**state, "slots": ceiling, "changed_at": now.astimezone(UTC).isoformat()})
            append_budget_history(
                root, now, "test_slots", state["slots"], ceiling,
                pressure=pressure, load_per_cpu=load_per_cpu, swapins=swapins, wanted=ceiling,
                load_average_per_cpu=load_average_per_cpu, cpu_busy=cpu_busy,
            )
            return ceiling
        current = state["slots"] if state else ceiling
        changed_at = datetime.fromisoformat(state["changed_at"]) if state else now
        wanted = next_test_slots(
            current, pressure="normal", load_per_cpu=load_per_cpu, ceiling=ceiling,
            increase_ok=now - changed_at >= INCREASE_INTERVAL,
        )
        if wanted < current and not decrease_allowed(state, now):
            wanted = current
        value = wanted
        if state is None or value != state["slots"]:
            updated = {"slots": value, "changed_at": now.astimezone(UTC).isoformat()}
            decreased_at = now.astimezone(UTC).isoformat() if value < current else (state or {}).get("decreased_at")
            if decreased_at is not None:
                updated["decreased_at"] = decreased_at
            _write_test_slots(root, updated)
        append_budget_history(
            root, now, "test_slots", state["slots"] if state else current, value,
            pressure=pressure, load_per_cpu=load_per_cpu, swapins=swapins, wanted=wanted,
            load_average_per_cpu=load_average_per_cpu, cpu_busy=cpu_busy,
        )
    return value


def _take_test_slot(limit: int) -> IO[str] | None:
    """Under the guard lock, hold one free slot file if fewer than limit are held."""
    root = machine_root()
    directory = root / TEST_SLOTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    with _flock(root / TEST_SLOTS_LOCK):
        indexes = [int(path.stem.removeprefix("slot-")) for path in directory.glob("slot-*.lock")
                   if path.stem.removeprefix("slot-").isdigit()]
        held, free = 0, None
        for index in range(max([limit - 1, *indexes]) + 1):
            stream = (directory / f"slot-{index}.lock").open("a", encoding="utf-8")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held += 1
                stream.close()
                continue
            if free is None and index < limit:
                free = stream
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
        if free is not None and held >= limit:
            fcntl.flock(free.fileno(), fcntl.LOCK_UN)
            free.close()
            free = None
        return free


@contextmanager
def hold_test_slot(ceiling: int, *, waiting: Callable[[], None] | None = None) -> Iterator[None]:
    """Hold one machine-wide test slot for one test command.

    A slot is an flock on a file, so the kernel releases it when the holder exits,
    including a crash. ``waiting`` runs when the wait starts and then periodically.
    """
    if os.environ.get(TEST_SLOT_ENV) == "held":
        # The parent command already holds a slot; taking another could deadlock.
        yield
        return
    heartbeat = None
    # While the kernel reports critical memory pressure, a free slot stays unclaimed.
    while kernel_pressure() == "critical" or (stream := _take_test_slot(effective_test_slots(ceiling))) is None:
        if waiting is not None and (heartbeat is None or time.monotonic() - heartbeat >= SLOT_HEARTBEAT_SECONDS):
            heartbeat = time.monotonic()
            waiting()
        time.sleep(SLOT_POLL_SECONDS)
    try:
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


SLEEP_HISTORY_FILE = "sleep-history.json"
# Sleep intervals are kept this long, and longer while a run that started earlier is running.
SLEEP_RETENTION = timedelta(days=30)
# A clock offset change below this is drift or rounding, not sleep.
SLEEP_MIN_SECONDS = 1.0
# Reads of the kernel pair and the clock offset repeat until both hold still.
SLEEP_SNAPSHOT_TRIES = 5


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int)]


def _darwin_sleep_interval() -> tuple[datetime, datetime] | None:
    """Read kern.sleeptime and kern.waketime in-process: the last sleep's wall times."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        times = []
        for name in (b"kern.sleeptime", b"kern.waketime"):
            value = _Timeval()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            if libc.sysctlbyname(name, ctypes.pointer(value), ctypes.pointer(size), None, ctypes.c_size_t(0)) != 0:
                return None
            times.append(datetime.fromtimestamp(value.tv_sec + value.tv_usec / 1e6, UTC))
    except (OSError, AttributeError, ValueError, OverflowError):
        return None
    return times[0], times[1]


def _sleep_offset() -> tuple[str, float]:
    """A clock difference that grows only while the machine sleeps.

    Linux CLOCK_MONOTONIC pauses in suspend and CLOCK_BOOTTIME does not. Python's
    monotonic clock pauses in sleep on macOS (mach_absolute_time) and Linux, so
    wall minus monotonic is the fallback.
    """
    try:
        return "boottime", time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return "wall", time.time() - time.monotonic()


def _clock_snapshot() -> tuple[tuple[datetime, datetime] | None, str, float, datetime] | None:
    """The kernel pair, clock offset and wall time as one consistent reading.

    A suspend between two of the reads would let one source see a sleep the
    other misses. Each reading is taken twice; it is used only when nothing moved.
    """
    for _ in range(SLEEP_SNAPSHOT_TRIES):
        pair = _darwin_sleep_interval() if sys.platform == "darwin" else None
        kind, offset = _sleep_offset()
        now = datetime.now(UTC)
        again = _darwin_sleep_interval() if sys.platform == "darwin" else None
        kind_again, offset_again = _sleep_offset()
        if pair == again and kind == kind_again and abs(offset_again - offset) < SLEEP_MIN_SECONDS:
            return pair, kind, offset, now
    return None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sleep_within(intervals: list[dict[str, Any]], since: datetime, until: datetime) -> float:
    """The least sleep that must lie inside [since, until].

    An interval says ``seconds`` of sleep happened somewhere in [start, end]. Only
    the part that cannot fit outside [since, until] is counted, so awake time
    near a coarse interval's edge is never counted as sleep.
    """
    total = 0.0
    for item in intervals:
        outside = (max(0.0, (min(since, item["end"]) - item["start"]).total_seconds())
                   + max(0.0, (item["end"] - max(until, item["start"])).total_seconds()))
        total += max(0.0, item["seconds"] - outside)
    return total


def _explained(intervals: list[dict[str, Any]], start: datetime, end: datetime) -> float:
    """The most recorded sleep that may lie inside [start, end]; used to avoid recording it twice."""
    return sum(min(item["seconds"], max(0.0, (min(item["end"], end) - max(item["start"], start)).total_seconds()))
               for item in intervals)


def _add_kernel_pair(intervals: list[dict[str, Any]], start: datetime, end: datetime) -> None:
    """Record an exact kernel sleep; coarse intervals that may hold it give that sleep up."""
    if any(item["start"] == start and item["end"] == end for item in intervals):
        return
    seconds = (end - start).total_seconds()
    left = seconds
    for item in intervals:
        if left <= 0:
            break
        share = min(item["seconds"], left, max(0.0, (min(item["end"], end) - max(item["start"], start)).total_seconds()))
        item["seconds"] -= share
        left -= share
    intervals[:] = [item for item in intervals if item["seconds"] > 0]
    intervals.append({"start": start, "end": end, "seconds": seconds})


def oldest_running_start(roots: set[Path]) -> datetime | None:
    """The earliest start among running runs in these stores and their sibling stores."""
    stores = set(roots)
    for parent in {root.parent for root in roots} | {machine_root()}:
        try:
            stores.update(path for path in parent.iterdir() if (path / "runs").is_dir())
        except OSError:
            continue  # No store has been created there yet.
    starts = []
    for root in stores:
        try:
            starts += [moment for run in StateStore(root).active_runs() if (moment := _when(run.started_at))]
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return min(starts, default=None)


def _read_history(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        raw = {}
    intervals = []
    for item in raw.get("intervals", []):
        start, end = _when(item.get("start")), _when(item.get("end"))
        if start and end and isinstance(item.get("seconds"), (int, float)):
            intervals.append({"start": start, "end": end, "seconds": float(item["seconds"])})
    return raw, intervals


def latest_wake(after: datetime) -> datetime | None:
    """The latest recorded moment by which the machine was awake again, if after ``after``.

    A kernel pair gives the measured wake; an interval found from a clock offset
    gives the read that found it, which is never before the wake.
    """
    root = machine_root()
    with _flock(root / f"{SLEEP_HISTORY_FILE}.lock"):
        _, intervals = _read_history(root / SLEEP_HISTORY_FILE)
    return max((item["end"] for item in intervals if item["end"] > after), default=None)


def _observe_sleep() -> tuple[list[dict[str, Any]], datetime]:
    """Add every sleep seen since the last read to the machine-wide history.

    The time and clocks are read inside the lock, so a reader that waited for
    another never writes an older sample over a newer one.
    """
    root = machine_root()
    path = root / SLEEP_HISTORY_FILE
    with _flock(root / f"{SLEEP_HISTORY_FILE}.lock"):
        raw, intervals = _read_history(path)
        snapshot = _clock_snapshot()
        if snapshot is None:  # The machine kept sleeping and waking; the next read records it.
            return intervals, datetime.now(UTC)
        pair, kind, offset, now = snapshot
        if pair is not None and pair[0] < pair[1] <= now:
            _add_kernel_pair(intervals, *pair)
        sample = raw.get("sample") if isinstance(raw.get("sample"), dict) else {}
        previous = _when(sample.get("at"))
        if (previous is not None and previous <= now and sample.get("kind") == kind
                and isinstance(sample.get("offset"), (int, float))):
            # Sleep the kernel's last pair missed, such as an earlier sleep between reads.
            missed = offset - sample["offset"] - _explained(intervals, previous, now)
            if missed >= SLEEP_MIN_SECONDS:
                intervals.append({"start": previous, "end": now, "seconds": missed})
        oldest = oldest_running_start(set())
        cutoff = min(now - SLEEP_RETENTION, oldest) if oldest else now - SLEEP_RETENTION
        intervals = [item for item in intervals if item["end"] >= cutoff]
        value = {
            "sample": {"at": now.isoformat(), "kind": kind, "offset": offset},
            "intervals": [{**item, "start": item["start"].isoformat(), "end": item["end"].isoformat()}
                          for item in sorted(intervals, key=lambda item: item["start"])],
        }
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{SLEEP_HISTORY_FILE}.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    return intervals, now


def slept_seconds(since: datetime) -> float:
    """Seconds the machine slept after ``since`` (an aware datetime); reading consumes nothing.

    Each read adds the sleep seen since the previous read to a machine-wide
    history: macOS's last kernel sleep and wake times, plus any growth of a
    sleep-counting clock offset that those times do not explain. A machine that
    was never read before ``since`` has no sleep recorded before its first read.
    """
    intervals, now = _observe_sleep()
    return sleep_within(intervals, since, now)


def slept_until(since: datetime, now: Callable[[], datetime]) -> tuple[float, datetime]:
    """Sleep in [since, now()] from one consistent reading, and that moment.

    ``now()`` is read between two equal sensor readings, so no sleep began
    between the reading and the moment; a suspend in between repeats the
    reading. If the machine keeps suspending, the later reading is used,
    capped at the window: never negative, never more than the window.
    """
    for _ in range(SLEEP_SNAPSHOT_TRIES):
        first = slept_seconds(since)
        moment = now()
        second = slept_seconds(since)
        if second - first < SLEEP_MIN_SECONDS:
            return first, moment
    return max(0.0, min(second, (moment - since).total_seconds())), moment

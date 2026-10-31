"""Machine-wide run budget and test slots: AIMD on CPU load (D60, #243).

Memory is a fact, not a budget input (#497): only the kernel's critical level
matters, and it holds new starts and new test slots without changing a budget.
"""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
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

# Inside observing(), machine_load() reads the sensors without writing their baselines.
_OBSERVING: ContextVar[bool] = ContextVar("foragent_capacity_observing", default=False)

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
        sample.disk_rates = dict.fromkeys(DISK_RATES.values())
        return sample


# Cumulative psutil disk counters and the per-second rate each one becomes.
DISK_RATES = {"read_bytes": "read_bytes_per_s", "write_bytes": "write_bytes_per_s",
              "read_count": "read_ops_per_s", "write_count": "write_ops_per_s"}


def _disk_rates(sensor: dict[str, Any]) -> dict[str, float | None]:
    """Disk I/O rates since the saved counters; rebases ``sensor`` on the current ones.

    The first reading, a counter reset (e.g. reboot) and an unreadable counter are unknown (None).
    """
    rates: dict[str, float | None] = dict.fromkeys(DISK_RATES.values())
    try:
        # nowrap=False: raw kernel counters, so a reset shows as a decrease instead of being
        # added onto the values psutil cached earlier in this process.
        counters = psutil.disk_io_counters(nowrap=False)
    except (OSError, RuntimeError, AttributeError, NotImplementedError, psutil.Error):
        counters = None
    if counters is None:
        sensor.pop("disk", None)
        return rates
    current = {key: getattr(counters, key) for key in DISK_RATES}
    current["at"] = time.time()
    previous = sensor.get("disk")
    if isinstance(previous, dict) and all(type(previous.get(key)) in (int, float) for key in current):
        elapsed = current["at"] - previous["at"]
        if elapsed > 0 and all(current[key] >= previous[key] for key in DISK_RATES):
            rates = {name: (current[key] - previous[key]) / elapsed for key, name in DISK_RATES.items()}
    sensor["disk"] = current
    return rates


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


def _busy_now(sensor: dict[str, Any]) -> tuple[float | None, dict[str, Any] | None]:
    """The busy fraction from the current CPU counters against the saved ones.

    Returns ``(busy, update)``: ``update`` is the new baseline to save, or None when
    the interval is too short and the saved fraction is reused, as the gate does.
    """
    times = psutil.cpu_times()
    # Linux guest time is already included in user/nice counters.
    total = sum(times) - getattr(times, "guest", 0) - getattr(times, "guest_nice", 0)
    idle = times.idle + getattr(times, "iowait", 0)
    previous_total, previous_idle = sensor.get("cpu_total"), sensor.get("cpu_idle")
    busy = None
    if (type(previous_total) in (int, float) and type(previous_idle) in (int, float)
            and 0 <= previous_idle <= previous_total):
        elapsed, idle_elapsed = total - previous_total, idle - previous_idle
        # Subtracting float counters can put an all-idle delta just above total.
        if elapsed >= 0 and 0 <= idle_elapsed <= elapsed + 2 * math.ulp(total):
            if elapsed < CPU_BUSY_MIN_SECONDS * (os.cpu_count() or 1):
                busy = sensor.get("cpu_busy")
                return (busy if type(busy) in (int, float) and 0 <= busy <= 1 else None), None
            busy = max(0.0, min(1.0, 1 - idle_elapsed / elapsed))
    # First samples and counter resets discard the old fraction and rebase.
    return busy, {"cpu_total": total, "cpu_idle": idle, "cpu_busy": busy}


def cpu_busy() -> float | None:
    """Busy fraction since the last persisted CPU sample; iowait is idle, steal is busy."""
    if os.environ.get("FORAGENT_CPU_BUSY") == "off":
        return None
    try:
        root = machine_root()
        with _flock(root / SENSOR_LOCK):
            sensor = _read_sensor(root)
            busy, update = _busy_now(sensor)
            if update is not None:
                sensor.update(update)
                (root / SENSOR_FILE).write_text(json.dumps(sensor), encoding="utf-8")
        return busy
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError, psutil.Error):
        return None


@contextmanager
def observing() -> Iterator[None]:
    """Read machine_load() for display only: no lock, no sensor baseline written."""
    token = _OBSERVING.set(True)
    try:
        yield
    finally:
        _OBSERVING.reset(token)


def _observed_load() -> LoadSample:
    """The sample the gate would take now, read-only: current counters against the saved ones.

    The saved fraction is reused only where the gate reuses it (a too-short interval);
    otherwise the busy fraction is measured now, so it matches the current load average.
    """
    busy = None
    # FORAGENT_CPU_BUSY=off turns the busy fraction off, including a previously saved one.
    if os.environ.get("FORAGENT_CPU_BUSY") != "off":
        try:
            busy, _ = _busy_now(_read_sensor(machine_root()))
        except (OSError, ValueError, TypeError, AttributeError, NotImplementedError, psutil.Error):
            busy = None
    raw_load = load_per_cpu()
    load = min(raw_load, busy / 0.6) if busy is not None else raw_load
    return LoadSample(kernel_pressure(), load, None, load_average_per_cpu=raw_load, cpu_busy=busy)


def machine_load() -> LoadSample:
    if _OBSERVING.get():
        return _observed_load()
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
        disk = _disk_rates(sensor)
        (root / SENSOR_FILE).write_text(json.dumps(sensor), encoding="utf-8")
    # Swap-ins are recorded as a fact; they never reclassify the kernel level.
    raw_load, busy = load_per_cpu(), cpu_busy()
    load = min(raw_load, busy / 0.6) if busy is not None else raw_load
    sample = LoadSample(pressure, load, swapins, load_average_per_cpu=raw_load, cpu_busy=busy)
    sample.disk_rates = disk
    return sample


def _within_grace(timestamp: str | None, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(timestamp) < LAUNCH_GRACE
    except (TypeError, ValueError):
        return False


def _alive(pid: int) -> bool:
    """The existing liveness rule: signal 0 probes only; a denied probe means alive.

    This process is alive by definition, so its own runs and workloads are never probed."""
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _holds_slot(run: Run, now: datetime) -> bool:
    if run.pid is None:
        # Between its record and its launch; after the grace it is a leftover.
        return _within_grace(run.started_at, now)
    return _alive(run.pid)


def _store_scan(
    store: StateStore, now: datetime, *, live: bool, candidate: Any = None,
) -> tuple[list[str], bool]:
    """One label per slot this store holds, and whether ``candidate`` reuses a held slot.

    ``live`` applies the liveness rule to every run; without it the store's own
    gate counts all of its active runs until its recovery clears them. A run or a
    reserved start already holds one slot for its own agent: the first live
    workload of that agent reuses it. The agent is identified by its pid (the run
    records this process) or, while the run has no pid yet, by the issue it reserved.
    ``candidate`` (a workload not yet written) is matched last, in the same scan,
    against the runs and reservations no recorded workload already reuses.
    """
    from foragent import capacity_sessions

    runs = store.active_runs()
    running = {run.number for run in runs}
    counted = [run for run in runs if _holds_slot(run, now)] if live else runs
    holders = [f"#{run.number}" for run in counted]
    # A start reserves its slot before the GitHub labels and the run record.
    starts = [number for number, prepared_at in store.run_starts().items()
              if number not in running and _within_grace(prepared_at, now)]
    holders += [f"#{number}" for number in starts]
    by_pid = Counter(run.pid for run in counted if run.pid is not None)
    by_issue = {run.number for run in counted if run.pid is None} | set(starts)

    def reuses(workload: Any) -> bool:
        if by_pid[workload.pid] > 0:
            by_pid[workload.pid] -= 1
            return True
        if workload.issue is not None and workload.issue in by_issue:
            by_issue.discard(workload.issue)
            return True
        return False

    live = [workload for workload in capacity_sessions.read(store.root) if _alive(workload.pid)]
    # A direct runner (no admitted run) holds its slot through a RUN_KIND workload: like a run
    # record with its pid, it covers its own first further workload (its first reviewer).
    for workload in sorted(live, key=lambda item: item.kind != RUN_KIND):
        if not reuses(workload):
            holders.append(workload.label())
            if workload.kind == RUN_KIND:
                by_pid[workload.pid] += 1
    return holders, candidate is not None and reuses(candidate)


def _store_holders(store: StateStore, now: datetime, *, live: bool) -> list[str]:
    return _store_scan(store, now, live=live)[0]


def _holds_anything(root: Path) -> bool:
    from foragent import capacity_sessions

    return any((root / name).is_dir() for name in ("runs", "run-starts", capacity_sessions.WORKLOADS_DIR))


def _machine_scan(
    store: StateStore, now: datetime, *, live: bool, candidate: Any = None,
) -> tuple[list[str], bool]:
    """Every held machine slot as "<project> <holder>", this store first; see _store_scan.

    Sibling stores always use the liveness rule; ``live`` applies it to this store too.
    A candidate only ever reuses a slot of its own store.
    """
    own, reused = _store_scan(store, now, live=live, candidate=candidate)
    holders = [f"{store.root.name} {label}" for label in own]
    for root in sorted(store.root.parent.iterdir()):
        if root == store.root or not _holds_anything(root):
            continue
        try:
            holders += [f"{root.name} {label}" for label in _store_holders(StateStore(root), now, live=True)]
        except (OSError, ValueError, TypeError, KeyError):
            continue  # Not a readable Foragent store; it cannot hold a slot.
    return holders, reused


def machine_holders(store: StateStore, now: datetime, *, live: bool = False) -> list[str]:
    """Label every held machine slot as "<project> <holder>", this store first."""
    return _machine_scan(store, now, live=live)[0]


def machine_runs(store: StateStore, now: datetime) -> int:
    """Count this store's active runs and the live runs and workloads of every sibling store."""
    return len(machine_holders(store, now))


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


@contextmanager
def _try_flock(path: Path) -> Iterator[bool]:
    """Like _flock, but one non-blocking try; yields whether it is held.

    A caller that must wait loops itself, so every wait is reported and bounded there.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
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


def effective_test_slots(
    ceiling: int, *, state: dict[str, Any] | None | bool = False, machine: dict[str, Any] | None = None,
) -> int:
    """The shared AIMD value, capped by machine.yml and this repository's lower ceiling.

    ``state`` and ``machine`` let a caller that already read test-slots.json and machine.yml
    reuse those values instead of reading the files again.
    """
    from foragent import machine_budget

    ceiling = min(ceiling, (machine if machine is not None else machine_budget.values())["test_slots"])
    if state is False:
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


def _take_test_slot(limit: int, *, wait: bool = True) -> IO[str] | None:
    """Under the guard lock, hold one free slot file if fewer than limit are held.

    ``wait=False`` tries the guard lock once; a busy guard reads as no free slot
    and the caller's loop does the (reported, bounded) waiting.
    """
    root = machine_root()
    directory = root / TEST_SLOTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    guard = _flock(root / TEST_SLOTS_LOCK) if wait else _try_flock(root / TEST_SLOTS_LOCK)
    with guard as locked:
        if locked is False:
            return None
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
def hold_test_slot(
    ceiling: int, *, waiting: Callable[[], None] | None = None, deadline: float | None = None,
    timed_out: Callable[[], str] | None = None,
) -> Iterator[None]:
    """Hold one machine-wide test slot for one test command.

    A slot is an flock on a file, so the kernel releases it when the holder exits,
    including a crash. ``waiting`` runs when the wait starts and then periodically.
    ``deadline`` (time.monotonic) bounds every wait, the guard lock included: the
    guard is then tried once per poll, so ``waiting`` runs before any wait. Past it,
    TimeoutError carries ``timed_out()``'s text; None waits until a slot frees.
    """
    if os.environ.get(TEST_SLOT_ENV) == "held":
        # The parent command already holds a slot; taking another could deadlock.
        yield
        return
    heartbeat = None
    # While the kernel reports critical memory pressure, a free slot stays unclaimed.
    while (kernel_pressure() == "critical"
           or (stream := _take_test_slot(effective_test_slots(ceiling), wait=deadline is None)) is None):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(timed_out() if timed_out is not None else "No machine test slot became free in time")
        if waiting is not None and (heartbeat is None or time.monotonic() - heartbeat >= SLOT_HEARTBEAT_SECONDS):
            heartbeat = time.monotonic()
            waiting()
        pause = SLOT_POLL_SECONDS if deadline is None else min(SLOT_POLL_SECONDS, deadline - time.monotonic())
        time.sleep(max(0.0, pause))
    taker = os.getpid()
    try:
        yield
    finally:
        # A forked child shares this open file and its lock: only the taker unlocks.
        # Closing the child's copy alone leaves the parent's lock in place.
        if os.getpid() == taker:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


# How often a waiting workload re-checks the machine budget and sensors.
WORKLOAD_POLL_SECONDS = 2.0
# The workload kind of a runner started without an admitted run; it holds that run's slot.
RUN_KIND = "run"
# A free slot behind a machine lock another admission holds for a moment is retried this soon.
LOCK_RETRY_SECONDS = 0.05
# The thread that holds each piece, with the workload record. A copied context
# carries these values into other threads (run_review's reviewers do that), so
# only the holding thread itself may reuse them: that is synchronous nesting.
_WORKLOAD: ContextVar[tuple[tuple[int, threading.Thread], Path] | None] = ContextVar(
    "foragent_capacity_workload", default=None)
# The thread that holds a test slot through hold_workload(tests=True).
_WORKLOAD_TESTS: ContextVar[tuple[int, threading.Thread] | None] = ContextVar(
    "foragent_capacity_workload_tests", default=None)


def _holder() -> tuple[int, threading.Thread]:
    """This process and thread. A forked child keeps its parent's Thread object and
    context values, so the pid is part of the identity: a child is a new agent."""
    return os.getpid(), threading.current_thread()


def _held_here(holder: tuple[int, threading.Thread] | None) -> bool:
    return holder is not None and holder[0] == os.getpid() and holder[1] is threading.current_thread()


def workload_budget(store: StateStore) -> int:
    """The shared AIMD value the gate keeps, capped by machine.yml's max_parallel."""
    from foragent import machine_budget

    state = read_state(store)
    value = int(state["parallel"]) if state else INITIAL_PARALLEL
    ceiling = machine_budget.values()["max_parallel"]
    return max(1, min(value, ceiling) if ceiling is not None else value)


def _sensor_block() -> str | None:
    """The existing holds on new work: critical kernel memory, then CPU overload."""
    with observing():  # Admission reads the sensors; the gate tick owns their baselines.
        load = machine_load()[1]
    if kernel_pressure() == "critical":
        return "critical"
    return "overload" if load >= OVERLOAD_LOAD else None


def _workload_text(
    key: str, language: str, reason: str, holders: list[str], budget: int, used: int | None = None, **values: Any,
) -> str:
    from foragent import i18n

    used = len(holders) if used is None else used
    return i18n.text(
        key, language, reason=i18n.text(f"workload_reason_{reason}", language),
        used=used, budget=budget, free=max(0, budget - used),
        holders=", ".join(holders) if holders else i18n.text("workload_holders_none", language), **values,
    )


def _say(message: str, wait_s: float) -> None:
    if wait_s > 0:  # A tick's single try defers quietly; a person learns why.
        print(message, file=sys.stderr, flush=True)


# One admission try at a time per process; the scan and record write inside are short.
_ADMISSION = threading.Lock()


def _acquire_workload(
    store: StateStore, record: Path, language: str, wait_s: float, deadline: float, *,
    kind: str, issue: int | None, role: str | None,
) -> None:
    """Write this process's workload ``record`` once a slot is free, by the monotonic ``deadline``.

    The run or reserved start of this same agent already holds its slot, so that
    workload is admitted at once whatever the budget and sensors say.
    """
    from foragent import capacity_sessions

    pid, shown, busy_since = os.getpid(), None, None
    candidate = capacity_sessions.Workload(Path(), pid, kind, issue, role)
    while True:
        sensed = _sensor_block()  # Read outside the lock: waiting holds no lock.
        # One try: the loop below does every wait, so each one is reported and bounded. Threads of
        # this process (concurrent reviewers) queue for the try, so they never see each other's
        # flock as another process's.
        with _ADMISSION, _try_flock(store.root.parent / LOCK_FILE) as locked:
            # One scan decides admission: reuse is matched by identity, never by comparing counts.
            holders, reused = _machine_scan(store, datetime.now(UTC), live=True, candidate=candidate)
            budget = workload_budget(store)
            reason = None if reused else sensed or ("full" if len(holders) >= budget else None)
            if locked and reason is None:
                capacity_sessions.add(store, record, pid=pid, kind=kind, issue=issue, role=role)
                return
        reason = reason or "busy"  # Free, but another process kept the machine lock past the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = _workload_text("workload_timeout", language, reason, holders, budget, seconds=f"{wait_s:g}")
            _say(message, wait_s)
            raise TimeoutError(message)
        # Concurrent admissions (N reviewers starting together) hold the lock only for one scan:
        # retry soon, and report the lock only once it stays busy for a whole poll interval.
        busy_since = (busy_since or time.monotonic()) if reason == "busy" else None
        brief = busy_since is not None and time.monotonic() - busy_since < WORKLOAD_POLL_SECONDS
        if not brief and (reason, tuple(holders)) != shown:
            shown = (reason, tuple(holders))
            _say(_workload_text("workload_wait", language, reason, holders, budget, seconds=f"{wait_s:g}"), wait_s)
        time.sleep(min(LOCK_RETRY_SECONDS if reason == "busy" else WORKLOAD_POLL_SECONDS, remaining))


def _held_test_slots() -> int:
    """How many slot files a process holds now; a probe takes and drops each free one at once."""
    held = 0
    for path in (machine_root() / TEST_SLOTS_DIR).glob("slot-*.lock"):
        with path.open("a", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held += 1
                continue
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return held


@contextmanager
def _workload_test_slot(store: StateStore, config: Any, language: str, wait_s: float, deadline: float):
    """The existing test slot, inside the same deadline and with the same localized guidance."""
    shown = None

    def facts() -> tuple[str, list[str], int, int]:
        """The real state now: the reason follows from the measured slots, not from the stage."""
        budget, used = effective_test_slots(config.test_slots), _held_test_slots()
        if kernel_pressure() == "critical":
            reason = "critical"
        elif used >= budget:
            reason = "tests"
        else:
            reason = "busy"  # A slot is free; another process held the slot guard lock.
        return reason, machine_holders(store, datetime.now(UTC), live=True), budget, used

    def waiting() -> None:
        nonlocal shown
        reason, holders, budget, used = facts()
        if (reason, tuple(holders), used) != shown:
            shown = (reason, tuple(holders), used)
            _say(_workload_text("workload_test_wait", language, reason, holders, budget, used,
                                seconds=f"{wait_s:g}"), wait_s)

    def timed_out() -> str:
        reason, holders, budget, used = facts()
        message = _workload_text("workload_test_timeout", language, reason, holders, budget, used,
                                 seconds=f"{wait_s:g}")
        _say(message, wait_s)
        return message

    with hold_test_slot(config.test_slots, waiting=waiting, deadline=deadline, timed_out=timed_out):
        yield


@contextmanager
def hold_workload(
    store: StateStore, config: Any, *, tests: bool = False, wait_s: float = 600,
    kind: str = "workload", issue: int | None = None, role: str | None = None,
) -> Iterator[None]:
    """Hold one machine slot for one agent workload; ``tests`` also holds a test slot.

    The slot is the shared gate budget: the same AIMD value, machine.yml ceiling,
    load and critical-memory holds, and the same count of live runs across
    stores. One ``wait_s`` budget of active (monotonic) seconds covers every wait
    of this acquisition: locks, the machine slot and the test slot. A tick passes
    0 and defers on TimeoutError. A synchronous nested hold in the same thread
    reuses the outer slot; concurrent agents (e.g. N reviewers in N threads, even
    with copied contexts) each hold one. Every exit releases what it took.
    """
    language = getattr(config, "language", "en") or "en"
    deadline = time.monotonic() + wait_s
    from foragent import capacity_sessions

    held = _WORKLOAD.get()
    holder, token = _holder(), None
    # The cleanup below owns the record's path before the record exists, so an
    # interruption at any point of the acquisition (Ctrl-C included) removes it.
    record = capacity_sessions.new_path(store, holder[0]) if held is None or not _held_here(held[0]) else None
    try:
        if record is not None:
            _acquire_workload(store, record, language, wait_s, deadline, kind=kind, issue=issue, role=role)
            token = _WORKLOAD.set((holder, record))
        if tests and not _held_here(_WORKLOAD_TESTS.get()):
            tests_token = None
            try:
                tests_token = _WORKLOAD_TESTS.set(holder)
                with _workload_test_slot(store, config, language, wait_s, deadline):
                    yield
            finally:
                if tests_token is not None:
                    _WORKLOAD_TESTS.reset(tests_token)
        else:
            yield
    finally:
        if token is not None:
            _WORKLOAD.reset(token)
        # A forked child unwinding this block leaves the parent's record to the parent.
        if record is not None and holder[0] == os.getpid():
            capacity_sessions.remove(record)


SLEEP_BASELINES_FILE = "sleep-baselines.json"
# A clock baseline is kept this long; a gate tick reads from its previous tick, minutes ago.
SLEEP_BASELINES_KEEP = timedelta(hours=48)


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


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _darwin_asleep_total() -> float | None:
    """Seconds asleep since boot: mach_continuous_time counts sleep, mach_absolute_time does not."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        continuous, absolute, timebase = libc.mach_continuous_time, libc.mach_absolute_time, _Timebase()
        continuous.restype = absolute.restype = ctypes.c_uint64
        if libc.mach_timebase_info(ctypes.pointer(timebase)) != 0 or not timebase.denom:
            return None
        return (continuous() - absolute()) * timebase.numer / timebase.denom / 1e9
    except (OSError, AttributeError, ValueError):
        return None


def _sleep_offset() -> tuple[str, float]:
    """A clock difference that grows only while the machine sleeps.

    Linux CLOCK_MONOTONIC pauses in suspend and CLOCK_BOOTTIME does not. Python's
    monotonic clock pauses in sleep, so wall minus monotonic is the fallback.
    """
    try:
        return "boottime", time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return "wall", time.time() - time.monotonic()


def _sleep_baseline(since: datetime, kind: str, offset: float) -> float:
    """The offset recorded at ``since``; the first read at a point records the current offset."""
    root = machine_root()
    path = root / SLEEP_BASELINES_FILE
    key = f"{kind}@{since.astimezone(UTC).isoformat()}"
    with _flock(root / f"{SLEEP_BASELINES_FILE}.lock"):
        try:
            baselines = json.loads(path.read_text(encoding="utf-8"))
            baselines = baselines if isinstance(baselines, dict) else {}
        except (OSError, ValueError):
            baselines = {}
        if isinstance(baselines.get(key), (int, float)):
            return float(baselines[key])
        baselines[key] = offset
        # Pruned by age only, never by which runs are running.
        cutoff = datetime.now(UTC) - SLEEP_BASELINES_KEEP
        keep = {name: value for name, value in baselines.items()
                if datetime.fromisoformat(name.split("@", 1)[1]) >= cutoff}
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{SLEEP_BASELINES_FILE}.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(keep, stream)
        os.replace(temporary, path)
    return offset


def slept_seconds(since: datetime) -> float:
    """Seconds the machine slept after ``since`` (an aware datetime); reading consumes nothing.

    macOS intersects the kernel's last sleep and wake times with [since, now] and
    also compares its sleep-since-boot clock with its value at ``since``.
    Elsewhere a sleep-counting clock offset is compared with its value at
    ``since``, recorded in the machine data root by the first read at that point.
    Only the gate tick reads it (``sleep_credit``), from the previous tick's time.
    """
    if sys.platform == "darwin":
        # The kernel's last sleep pair, and the cumulative sleep since boot: the pair alone
        # forgets an earlier sleep once the machine sleeps again.
        interval = _darwin_sleep_interval()
        latest = 0.0
        if interval is not None and interval[0] < interval[1]:
            latest = max(0.0, (min(interval[1], datetime.now(UTC)) - max(interval[0], since)).total_seconds())
        total = _darwin_asleep_total()
        if total is None:
            return latest
        return max(latest, total - _sleep_baseline(since, "mach", total))
    kind, offset = _sleep_offset()
    return max(0.0, offset - _sleep_baseline(since, kind, offset))

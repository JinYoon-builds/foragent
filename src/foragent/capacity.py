"""Machine-wide run budget: AIMD on memory pressure and load (D60)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from foragent.state import Run, StateStore

# Below this load per CPU the machine has headroom for one more run.
HEADROOM_LOAD = 1.0
# At or above this load per CPU runs slow each other down; halve at once.
OVERLOAD_LOAD = 1.5
# The budget before any measurement; swapping started at 3+ full test suites.
INITIAL_PARALLEL = 2
# A new run takes minutes to grow its memory; space additive increases.
INCREASE_INTERVAL = timedelta(minutes=2)
# A run without a pid, or a reserved start, may still launch for this long.
LAUNCH_GRACE = timedelta(minutes=2)
STATE_FILE = "capacity.json"
LOCK_FILE = "capacity.lock"

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


def memory_pressure() -> str:
    """Return normal, warn or critical; an unreadable sensor reads as normal."""
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout
            return _DARWIN_PRESSURE.get(int(output.strip()), "normal")
        except (OSError, ValueError, subprocess.SubprocessError):
            return "normal"
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


def load_per_cpu() -> float:
    """One-minute load average divided by the CPU count (vm.loadavg, /proc/loadavg)."""
    try:
        load = os.getloadavg()[0]
    except OSError:
        return 0.0
    return max(0.0, load) / (os.cpu_count() or 1)


def machine_load() -> tuple[str, float]:
    return memory_pressure(), load_per_cpu()


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
    root = store.root.parent
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_FILE).open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_state(store: StateStore) -> dict[str, Any] | None:
    try:
        value = json.loads((store.root.parent / STATE_FILE).read_text(encoding="utf-8"))
        int(value["parallel"]), datetime.fromisoformat(value["changed_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return value


def write_state(store: StateStore, parallel: int, changed_at: datetime) -> None:
    store._write_json(store.root.parent / STATE_FILE, {
        "parallel": parallel, "changed_at": changed_at.astimezone(UTC).isoformat(),
    })

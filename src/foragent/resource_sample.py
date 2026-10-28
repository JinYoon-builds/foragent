"""One machine resource sample per gate tick, kept beside the sensor file (D93).

The gate writes it; `fa orch facts` only reads it. Facts only: nothing here
changes a budget or decides anything.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from foragent import capacity
from foragent.state import Run, StateStore

PREFIX = "resource-sample-"  # One file per repository: resource-sample-<store>.json
FIELDS = ("at", "cpu_busy", "load_average_per_cpu", "memory_pressure", *capacity.DISK_RATES.values(),
          "test_slot_queue")


def waiting_for_slot(run: Run, now: datetime) -> bool:
    """A running execution queued for a test slot now: its slot wait is still refreshed."""
    if run.status != "running" or run.phase != "test-slot-wait":
        return False
    try:
        return datetime.fromisoformat(run.waiting_until) > now
    except (TypeError, ValueError):
        return False


def machine_stores(store: StateStore) -> list[Path]:
    """The other repository stores under the machine root: test slots are machine-wide.

    The gate's sample and `fa orch facts` both count test-slot waiters in ``store``
    plus these, so the two never disagree about which stores exist.
    """
    try:
        return sorted(path for path in capacity.machine_root().iterdir()
                      if path != store.root and (path / "runs").is_dir())
    except OSError:
        return []


def slot_queue(store: StateStore, now: datetime) -> int:
    """Runs waiting for a test slot in this store and every store under the machine root."""
    count = 0
    for root in [store.root, *machine_stores(store)]:
        try:
            runs = (store if root == store.root else StateStore(root, now=lambda: now)).active_runs()
        except (OSError, ValueError, TypeError, KeyError):
            continue
        count += sum(waiting_for_slot(run, now) for run in runs)
    return count


def path_for(store: StateStore) -> Path:
    """This repository's own sample file: its gate is the file's only writer (D95)."""
    return capacity.machine_root() / f"{PREFIX}{store.root.name}.json"


def record(store: StateStore, sample: tuple[str, float], at: datetime) -> None:
    """Replace this repository's sample with this tick's readings (no new sensor read).

    ``at`` is when the readings were taken, before the sensors were read. Each gate
    writes only its own file, so a tick delayed after measuring cannot replace another
    repository's newer sample; readers take the newest ``at`` across files (latest()).
    """
    load = getattr(sample, "load_average_per_cpu", None)
    row = {
        "at": at.isoformat(), "cpu_busy": getattr(sample, "cpu_busy", None),
        "load_average_per_cpu": load if load is not None else sample[1], "memory_pressure": sample[0],
        **(getattr(sample, "disk_rates", None) or dict.fromkeys(capacity.DISK_RATES.values())),
        "test_slot_queue": slot_queue(store, at),
    }
    target = path_for(store)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(row, stream)
        os.replace(temporary, target)
    except OSError:
        pass  # A missed sample leaves the previous one, whose age facts report.


def latest(directory: Path) -> dict[str, Any] | None:
    """The newest valid sample by its measured ``at`` among the per-repository files in ``directory``."""
    rows = [row for path in sorted(directory.glob(f"{PREFIX}*.json")) if (row := read(path)) is not None]
    return max(rows, key=lambda row: datetime.fromisoformat(row["at"]), default=None)


def read(path: Path) -> dict[str, Any] | None:
    """The stored sample, or None when there is none or it is unreadable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or not set(FIELDS) <= value.keys()
            or not isinstance(value["memory_pressure"], str)
            or type(value["load_average_per_cpu"]) not in (int, float)):
        return None
    try:
        datetime.fromisoformat(value["at"])
    except (TypeError, ValueError):
        return None
    return {key: value[key] for key in FIELDS}

"""Step measurements owned by the StateStore transition path under state.lock.

Boundaries start/finish measurements even without a gate tick inside a phase.
Gate observations use the same locked writer for reported peaks and a partial
measurement; the completed phase replaces its prefix. Readers derive medians.
"""

from datetime import datetime
import json
import math
import os
from pathlib import Path
from statistics import median
import tempfile

import psutil

from foragent import capacity, step_weights_usage as usage

STEPS = ("implementing", "reviewing", "checking")
WINDOW = 128


def path_for(store) -> Path:
    return capacity.machine_root() / f"step-weights-{store.root.name}.json"


def _read(store) -> dict:
    try:
        value = json.loads(path_for(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # v1's per-PID interval samples omitted reaped CPU; do not mix them into v2.
    if not isinstance(value, dict) or value.get("version") != 2:
        return {}
    if not all(isinstance(value.get(key, {}), dict) for key in ("observations", "open", "tracked")):
        return {}
    return value


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _root(run, saved):
    birth = saved.get("tracked", {}).get(str(run.key), {})
    identities = run.process_identities
    if not identities and isinstance(birth, dict) and birth.get("started_at") == run.started_at:
        identities = birth.get("identities", [])
    for item in identities:
        if item.get("pid") == run.pid:
            return item
    if run.pid == os.getpid():
        try:
            return {"pid": run.pid, "start_time": psutil.Process(run.pid).create_time()}
        except (OSError, psutil.Error):
            pass
    return None


def _capture(run, saved):
    root = _root(run, saved)
    return usage.snapshot(root, run.process_identities) if root else None


def _open(run, phase, at, sample):
    return {"execution": run.started_at, "phase": phase, "at": at.isoformat(),
            "id": json.dumps([run.key, run.started_at, phase, at.isoformat()]),
            "start": sample, "last": sample, "peak": usage.memory(sample)}


def _matches(scope, run):
    return (isinstance(scope, dict) and scope.get("execution") == run.started_at
            and scope.get("phase") == run.phase)


def _observe(saved, scope, sample, at, *, complete=False):
    start = scope.get("start")
    if not isinstance(start, dict) or not isinstance(sample, dict):
        return
    elapsed = (at - datetime.fromisoformat(scope["at"])).total_seconds()
    if elapsed <= 0:
        return
    delta = sample["cpu"] - start["cpu"]
    valid = delta >= 0 and sample["cpu"] >= scope["last"]["cpu"]
    scope["peak"] = max(scope["peak"], usage.memory(sample, before=at.timestamp() if complete else None))
    rows = saved.setdefault("observations", {}).setdefault(scope["phase"], [])
    previous = next((row for row in rows if row["id"] == scope["id"]), None)
    row = {"id": scope["id"], "at": at.isoformat(), "cpu_cores": delta / elapsed if valid else None,
           "memory_bytes": scope["peak"], "complete": complete}
    if not valid and previous:
        row["cpu_cores"] = previous["cpu_cores"]
    rows[:] = [item for item in rows if item["id"] != row["id"]] + [row]
    rows[:] = rows[-WINDOW:]
    scope["last"] = sample


def transition(store, run, at: datetime, next_step: str) -> None:
    """Close/open resource scopes at the actual boundary. Caller holds state.lock."""
    if run.phase not in STEPS and next_step not in STEPS:
        return
    saved = _read(store)
    sample = _capture(run, saved)
    if sample is None:
        return
    scopes = saved.setdefault("open", {})
    key = str(run.key)
    old = scopes.get(key)
    if _matches(old, run):
        _observe(saved, old, sample, at, complete=True)
    if next_step in STEPS:
        scopes[key] = _open(run, next_step, at, sample)
    else:
        scopes.pop(key, None)
    _save(store, {**saved, "version": 2})


def record(store) -> None:
    """Add gate observations without changing cadence, admission or stop ownership."""
    with store._state_lock():
        runs, at = store.active_runs(), store._now()
        saved = _read(store)
        scopes = saved.setdefault("open", {})
        active = set()
        for run in runs:
            key = str(run.key)
            active.add(key)
            if run.status != "running" or run.phase not in STEPS:
                continue
            sample = _capture(run, saved)
            if sample is None:
                continue
            old = scopes.get(key)
            if _matches(old, run):
                _observe(saved, old, sample, at)
            else:
                scopes[key] = _open(run, run.phase, at, sample)
        saved["open"] = {key: value for key, value in scopes.items() if key in active}
        saved["tracked"] = {key: value for key, value in saved.get("tracked", {}).items() if key in active}
        if saved.get("observations") or saved["open"] or saved["tracked"]:
            _save(store, {**saved, "version": 2})


def started(store, run) -> None:
    """Pin a newly launched runner for telemetry only; never grants stop ownership."""
    identities = run.process_identities
    if not identities and run.pid is not None:
        try:
            start = psutil.Process(run.pid).create_time()
        except (psutil.Error, OSError):
            return
        if start < datetime.fromisoformat(run.started_at).timestamp():
            return
        identities = [{"pid": run.pid, "start_time": start}]
    if not identities:
        return
    with store._state_lock():
        saved = _read(store)
        saved.setdefault("tracked", {})[str(run.key)] = {"started_at": run.started_at, "identities": identities}
        _save(store, {**saved, "version": 2})


def _save(store, value: dict) -> None:
    target, temporary = path_for(store), None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, target)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def learned(store) -> dict:
    observations = _read(store).get("observations", {})
    result = {}
    for step in STEPS:
        rows = observations.get(step, [])
        cpu = [row["cpu_cores"] for row in rows if _number(row.get("cpu_cores"))]
        memory = [row["memory_bytes"] for row in rows if _number(row.get("memory_bytes"))]
        result[step] = {"cpu_cores": median(cpu) if cpu else None,
                        "memory_bytes": median(memory) if memory else None,
                        "cpu_samples": len(cpu), "memory_samples": len(memory),
                        "last_observed_at": rows[-1]["at"] if rows else None}
    return result


def human(weights: dict) -> str:
    parts = []
    for step in STEPS:
        item = weights[step]
        cpu, memory = item["cpu_cores"], item["memory_bytes"]
        parts.append(f"{step} " + ("?" if cpu is None else f"{cpu:.2f} CPU") + "/"
                     + ("?" if memory is None else f"{memory / 1024**3:.2f} GiB"))
    return "  Step weights (CPU average/reported memory high water): " + "; ".join(parts)

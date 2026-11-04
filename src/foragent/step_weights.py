"""Gate-owned, machine/project-local observations of run step resource use.

The gate samples identities already witnessed by its process-tree recovery pass.
CPU cores are delta(user + system CPU seconds) / elapsed wall seconds, never
children_user/system (which would count descendants twice). Memory is summed RSS
bytes, including detached descendants whose birth is still recorded. RSS is a
conservative footprint: shared pages may be counted in more than one process.

One gate under its tick lock writes this project's file atomically. Keep the
latest 128 observations per step plus the counters needed for the next interval;
readers derive median weights, without probing processes or changing admission.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
from statistics import median
import tempfile

import psutil

from foragent import capacity, processes

STEPS = ("implementing", "reviewing", "checking")
WINDOW = 128


def path_for(store) -> Path:
    return capacity.machine_root() / f"step-weights-{store.root.name}.json"


def _read(store) -> dict:
    try:
        value = json.loads(path_for(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _tree(identities) -> dict | None:
    """Read only matching lifetimes; an unreadable live member voids the sample."""
    members = {}
    for identity in identities:
        pid, start = identity["pid"], identity["start_time"]
        try:
            process = psutil.Process(pid)
            if process.create_time() != start or process.status() == psutil.STATUS_ZOMBIE:
                continue
            times = process.cpu_times()
            cpu, memory = times.user + times.system, process.memory_info().rss
            if psutil.Process(pid).create_time() != start:
                return None  # Reuse during the read: none of this interval is trustworthy.
        except psutil.NoSuchProcess:
            continue  # Ended descendants are kept in the ownership record, but use no RSS now.
        except (psutil.Error, OSError):
            return None
        if not _number(cpu) or not _number(memory):
            return None
        members[str(pid)] = {"start": start, "cpu": cpu, "memory": memory}
    return members or None


def _cpu(current: dict, previous: dict, at: float) -> float | None:
    elapsed = at - previous["at"]
    if elapsed <= 0 or previous["members"].keys() - current.keys():
        # A reaped process's final counter is unavailable. Do not teach the
        # remaining processes' partial CPU use as the whole tree's weight.
        return None
    seconds = 0
    for pid, member in current.items():
        old = previous["members"].get(pid)
        if old is not None and old["start"] == member["start"]:
            delta = member["cpu"] - old["cpu"]
        elif member["start"] >= previous["at"]:
            delta = member["cpu"]  # A child born within this interval starts at zero.
        else:
            return None  # No counter for an older, newly readable member: rebase first.
        if delta < 0:
            return None
        seconds += delta
    return seconds / elapsed


def record(store, runs, at: datetime) -> None:
    """Called only by the gate after its ownership refresh; never changes run records."""
    saved = _read(store)
    observations = saved.get("observations", {})
    previous = saved.get("previous", {})
    current, tracked = {}, {}
    for run in runs:
        if run.status != "running":
            continue
        key = str(run.key)
        birth = saved.get("tracked", {}).get(key, {})
        identities = run.process_identities
        if not identities and birth.get("started_at") == run.started_at:
            identities = processes.observe_routine_tree(birth["identities"])
        tracked[key] = {"started_at": run.started_at, "identities": identities}
        if run.phase not in STEPS:
            continue
        members = _tree(identities)
        if members is None:
            continue
        phase = [run.started_at, run.phase, run.phase_started_at]
        old = previous.get(key)
        if old is not None and old["at"] >= at.timestamp():
            current[key] = old
            continue  # A repeated tick clock cannot create another observation.
        cpu = _cpu(members, old, at.timestamp()) if old and old["phase"] == phase else None
        current[key] = {"at": at.timestamp(), "phase": phase, "members": members}
        observation = {"at": at.isoformat(), "cpu_cores": cpu,
                       "memory_bytes": sum(member["memory"] for member in members.values())}
        observations[run.phase] = [*observations.get(run.phase, []), observation][-WINDOW:]
    if not saved and not current:
        return  # An unmeasured project stays unmeasured.
    _save(store, {"version": 1, "observations": observations, "previous": current, "tracked": tracked})


def started(store, run) -> None:
    """Pin a launched runner's lifetime for telemetry, without granting stop ownership.

    The native launcher already records birth evidence. An injected launcher may
    return only a PID; witness that birth here, before a later tick can see a reused
    number. Descendants are then discovered by the existing routine tree observer.
    """
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
    saved = _read(store)
    saved.setdefault("tracked", {})[str(run.key)] = {"started_at": run.started_at, "identities": identities}
    _save(store, {**saved, "version": 1})


def _save(store, value: dict) -> None:
    target = path_for(store)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, target)
    except OSError:
        pass  # Telemetry failure leaves the last learned weights and admission intact.
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def learned(store) -> dict:
    """Read-only medians; unknown is None, never a fabricated default weight."""
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
    return "  Step weights (median tree RSS): " + "; ".join(parts)

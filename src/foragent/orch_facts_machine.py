"""The resources `fa orch facts` reads from local records only (D93): no sensor, executor or gh call."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from collections import Counter
import json
from pathlib import Path
import shutil
from typing import Any

from foragent import capacity, capacity_sessions, resource_sample
from foragent.config import Config, lane_executors
from foragent.i18n import text
from foragent.state import Run, StateStore


SLOT_STORES = "slot-stores"
SAMPLES = "resource-samples"
MERGE_STORES = "merge-stores"


def freeze_slot_store(store: StateStore, source: Path, into: Path, copy: Callable[[Path, Path], None]) -> None:
    """Copy a machine-root store's run records for the test-slot queue, as the gate's sample counts them."""
    if source != store.root and source.parent == capacity.machine_root():
        for path in (source / "runs").glob("*.json"):
            copy(path, into / SLOT_STORES / source.name / "runs" / path.name)


def merge_store_roots(store: StateStore) -> set[Path]:
    """The other stores the ETA's machine-wide merge samples read (progress.machine_stores)."""
    try:
        return {path for path in capacity.machine_root().iterdir() if path != store.root and path.is_dir()
                and ((path / "runs.jsonl").exists() or (path / "issues.json").exists())}
    except OSError:
        return set()


def copy_reviews(source: Path, target: Path) -> None:
    """Review artifacts keep their modification time: it places each in its execution."""
    for path in (source / "runs").glob("issue-*/review-*.txt"):
        (target / "runs" / path.parent.name).mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target / "runs" / path.parent.name / path.name)
        except FileNotFoundError:
            pass


def freeze_merge_store(store: StateStore, source: Path, into: Path, copy: Callable[[Path, Path], None]) -> None:
    """Copy what another store's merge samples and their review rounds read."""
    if source not in merge_store_roots(store):
        return
    target = into / MERGE_STORES / source.name
    for name in ("issues.json", "runs.jsonl", "orch-events.jsonl"):
        copy(source / name, target / name)
    for path in (source / "runs").glob("*.json"):
        copy(path, target / "runs" / path.name)
    copy_reviews(source, target)


def merge_stores(machine: dict[str, Any], now: datetime) -> list[StateStore]:
    """The frozen copies freeze_merge_store made."""
    try:
        roots = sorted(path for path in (machine["merge_stores"]).iterdir() if path.is_dir())
    except (KeyError, OSError):
        return []
    return [StateStore(root, now=lambda: now) for root in roots]


def probe_workloads(frozen: Path, probe: Callable[[int], bool]) -> dict[int, bool]:
    """Whether each frozen workload's process lives, read while its record cannot change."""
    return {workload.pid: probe(workload.pid) for root in frozen.iterdir() if root.is_dir()
            for workload in capacity_sessions.read(root)}


def workload_slots(root: Path, counted: list[Run], starts: list[int], alive: dict[int, bool]) -> int:
    """Slots a store's workloads hold beyond its runs and reservations: capacity._store_scan's rule, unsignalled."""
    by_pid = Counter(run.pid for run in counted if run.pid is not None)
    by_issue = {run.number for run in counted if run.pid is None} | set(starts)
    live = [workload for workload in capacity_sessions.read(root) if alive.get(workload.pid, True)]
    slots = 0
    # A run or reservation already holds one slot for its agent's first workload.
    for workload in sorted(live, key=lambda item: item.kind != capacity.RUN_KIND):
        if by_pid[workload.pid] > 0:
            by_pid[workload.pid] -= 1
        elif workload.issue is not None and workload.issue in by_issue:
            by_issue.discard(workload.issue)
        else:
            slots += 1
            if workload.kind == capacity.RUN_KIND:
                by_pid[workload.pid] += 1
    return slots


def freeze_sample(into: Path, copy: Callable[[Path, Path], None]) -> tuple[dict[str, Any] | None, tuple[str, float]]:
    """The newest gate sample on the machine and its (pressure, load); without one, today's live reading."""
    for path in capacity.machine_root().glob(f"{resource_sample.PREFIX}*.json"):
        copy(path, into / SAMPLES / path.name)
    stored = resource_sample.latest(into / SAMPLES)
    if stored is None:
        with capacity.observing():
            return None, capacity.machine_load()
    return stored, (stored["memory_pressure"], float(stored["load_average_per_cpu"]))


def sample(machine: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The gate's latest sample and its age; every field is None when the gate recorded none."""
    stored = machine.get("stored")
    if stored is None:
        return {**dict.fromkeys(resource_sample.FIELDS), "age_seconds": None}
    return {**stored, "age_seconds": (now - datetime.fromisoformat(stored["at"])).total_seconds()}


def _executors(store: StateStore, config: Config, now: datetime) -> dict[str, dict[str, Any]]:
    # Every configured implementer and reviewer, as each lane resolves them.
    names = set().union(*(lane_executors(config, lane) for lane in ("main", *config.lanes)))
    try:
        pauses = json.loads((store.root / "executor-pauses.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pauses = {}
    names |= set(pauses if isinstance(pauses, dict) else ()) | set(store.usage())
    result = {}
    for name in sorted(name for name in names if name):
        until = store.executor_pause_until(name, now)
        result[name] = ({"state": "limited", "until": until.isoformat()} if until is not None
                        else {"state": "ok", "until": None})
    return result


def _github(store: StateStore) -> dict[str, Any] | None:
    """The remaining core requests GitHub reported when the gate last asked, with that time."""
    try:
        value = json.loads((store.root / "github-rate-limit.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(value, dict) or type(value.get("remaining")) is not int or not isinstance(
            value.get("seen_at"), str):
        return None
    return {"remaining": value["remaining"], "seen_at": value["seen_at"]}


def _slot_queue(store: StateStore, machine: dict[str, Any], now: datetime, running: list[Run]) -> int:
    """Test-slot waiters in this store and the machine-root stores (resource_sample.slot_queue).

    Facts read the frozen copies; the tick's binding (no frozen read) reads the same stores live.
    """
    count = sum(resource_sample.waiting_for_slot(run, now) for run in running)
    if (frozen := machine.get("slot_stores")) is None:
        roots = resource_sample.machine_stores(store)
    else:
        try:
            roots = sorted(path for path in frozen.iterdir() if path.is_dir())
        except OSError:
            roots = []
    for root in roots:
        try:
            runs = StateStore(root, now=lambda: now).active_runs()
        except (OSError, ValueError, TypeError, KeyError):
            continue
        count += sum(resource_sample.waiting_for_slot(run, now) for run in runs)
    return count


def local(store: StateStore, config: Config, now: datetime, running: list[Run],
          machine: dict[str, Any]) -> dict[str, Any]:
    return {"executors": _executors(store, config, now), "github_api_remaining": _github(store),
            "test_slot_queue": _slot_queue(store, machine, now, running)}


def _value(value: Any, unknown: str, form: str = "{}") -> str:
    return unknown if value is None else form.format(value)


def human_line(resources: dict[str, Any], language: str) -> str:
    """One line; a value with no record says unknown rather than looking idle."""
    machine, unknown = resources["machine"], text("facts_resources_unknown", language)
    rates = [machine[name] for name in capacity.DISK_RATES.values()]
    disk = unknown if any(rate is None for rate in rates) else text(
        "facts_resources_disk", language, read=rates[0] / 1024, write=rates[1] / 1024,
        read_ops=rates[2], write_ops=rates[3])
    github = resources["github_api_remaining"]
    executors = ", ".join(
        f"{name} " + (text("facts_resources_limited", language, until=value["until"]) if value["state"] == "limited"
                      else text("facts_resources_ok", language))
        for name, value in resources["executors"].items()) or unknown
    return text(
        "facts_resources", language,
        cpu=_value(machine["cpu_busy"], unknown, "{:.0%}"),
        load=_value(machine["load_average_per_cpu"], unknown, "{:.2f}"),
        memory=_value(machine["memory_pressure"] if machine["at"] is not None else None, unknown),
        disk=disk, github=_value(github and github["remaining"], unknown),
        queue=resources["test_slot_queue"], executors=executors,
        age=_value(machine["age_seconds"], unknown, "{:.0f} s"),
    )

"""The resources `fa orch facts` reads from local records only (D93): no sensor, executor or gh call."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from foragent import capacity, resource_sample
from foragent.config import Config, lane_executors
from foragent.i18n import text
from foragent.state import Run, StateStore


SLOT_STORES = "slot-stores"
SAMPLES = "resource-samples"


def freeze_slot_store(store: StateStore, source: Path, into: Path, copy: Callable[[Path, Path], None]) -> None:
    """Copy a machine-root store's run records for the test-slot queue, as the gate's sample counts them."""
    if source != store.root and source.parent == capacity.machine_root():
        for path in (source / "runs").glob("*.json"):
            copy(path, into / SLOT_STORES / source.name / "runs" / path.name)


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

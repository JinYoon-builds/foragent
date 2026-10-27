"""The binding constraint over time: the tick appends one ``binding`` event when it changes.

Events go to ``<state>/binding-events.jsonl`` (append-only, beside the orch journal) so
the orchestrator's action log keeps only actions.

The tick is the only writer. It works the constraint out from what it already holds
(its memory sample, run hours, the local runs and issues), in the order
``orch_facts_resources._binding`` checks it, without probing processes again. The
scorecard reads the latest event at or before a moment.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from foragent import capacity
from foragent.config import Config, in_run_hours
from foragent.orch_facts_resources import _jsonl, _time
from foragent.state import StateStore

NONE = "none"
FILE = "binding-events.jsonl"


def at(store: StateStore, moment: datetime) -> str | None:
    """The binding recorded last at or before ``moment``; None when no event is that old."""
    latest = None
    for event in _jsonl(store.root / FILE):
        when = _time(event.get("at"))
        if event.get("kind") == "binding" and when is not None and when <= moment:
            if latest is None or when >= latest[0]:
                latest = (when, event.get("binding") or NONE)
    return latest[1] if latest else None


def from_tick(store: StateStore, config: Config, now: datetime, sample: Any, github_wait: bool) -> str:
    """The tick's view of the one resource holding work now."""
    running = [run for run in store.active_runs() if run.status == "running"]
    busy = {run.number for run in running} | set(store.run_starts())
    waiting = [row for row in store.local_issues() if row.get("state") == "open"
               and {"approved", "ready"} <= set(row.get("labels") or ())
               and not set(row.get("labels") or ()) & {"blocked", "needs-human", "running"}
               and row["number"] not in busy]
    slot_wait = any(run.phase == "test-slot-wait" for run in running)
    overlap_wait = any(run.phase == "overlap-wait" for run in running)
    if not waiting and not slot_wait and not overlap_wait:
        return NONE
    pressure, load = (sample[0], float(sample[1])) if sample else ("normal", 0.0)
    if pressure == "critical" or load >= capacity.OVERLOAD_LOAD:
        return "machine"
    if github_wait:
        return "github"
    if not in_run_hours(now.astimezone(), config.run_hours):
        return "run_hours"
    if slot_wait:
        return "test_slots"
    if waiting and not overlap_wait:
        state = capacity.read_state(store)
        cap = int(state["parallel"]) if state else capacity.INITIAL_PARALLEL
        if config.max_parallel is not None:
            cap = min(cap, config.max_parallel)
        if len(busy) >= cap:
            return "cap"
    return "structure"


def record(store: StateStore, now: datetime, binding: str) -> bool:
    """Append one ``binding`` event when ``binding`` differs from the last one recorded."""
    with store._state_lock():
        recorded = [event for event in _jsonl(store.root / FILE) if event.get("kind") == "binding"]
        if recorded and (recorded[-1].get("binding") or NONE) == binding:
            return False  # One writer appends in time order: the last line is the latest.
        store.root.mkdir(parents=True, exist_ok=True)
        event = {"at": now.astimezone(UTC).isoformat(), "kind": "binding", "binding": binding}
        with (store.root / FILE).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
    return True


def record_tick(ctx: Any) -> bool:
    """The tick's one call: its own time, config, memory sample and GitHub wait; no extra probes."""
    now = ctx.now()
    github_wait = ctx.store.github_wait_until(now) is not None
    kind = from_tick(ctx.store, ctx.config, now, getattr(ctx, "_budget_sample", None), github_wait)
    return record(ctx.store, now, kind)

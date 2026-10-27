"""The binding constraint over time: the tick appends one ``binding`` event when it changes.

Events go to ``<state>/binding-events.jsonl`` (append-only, beside the orch journal) so
the orchestrator's action log keeps only actions.

The tick is the only writer. It runs fa orch facts' own binding functions over what it
already holds (its memory sample and the local records), without probing processes
again. The scorecard reads the latest event at or before a moment, and replays facts'
start loop from that day's records when no event is that old
(``orch_scorecard_flow.binding_at``).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from foragent import capacity
from foragent.config import Config
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


def from_tick(store: StateStore, config: Config, root, now: datetime, sample: Any) -> str:
    """fa orch facts' own binding (``_resources`` then ``_binding``) over the tick's local records.

    The tick passes its memory sample; sibling runs count as alive (no process probe), as
    in facts' frozen read when a pid was not probed."""
    from foragent import machine_budget
    from foragent.orch_facts_resources import _binding, _resources, _still_running
    from foragent.orch_facts_runs import _test_slots, _waiting_issues

    running = _still_running(store, store.active_runs())
    machine = {"sample": tuple(sample) if sample else ("normal", 0.0), "alive": {},
               "budget": machine_budget.values(), "test_slots": capacity.read_test_slots()}
    resources = _resources(store, config, root, now, _waiting_issues(store, config, running),
                           _test_slots(config, running, [], machine), running, machine)
    found = _binding(resources)
    return found["kind"] if found else NONE


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
    """The tick's one call: its own time, config, checkout and memory sample; no extra probes."""
    now = ctx.now()
    kind = from_tick(ctx.store, ctx.config, ctx.repo_root, now, getattr(ctx, "_budget_sample", None))
    return record(ctx.store, now, kind)

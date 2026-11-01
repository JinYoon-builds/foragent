"""A lane's preferred start order, kept in the plan (``<state>/plan.json``, see ``orch_plan``).

The queue is only a preference. The tick still skips issues that are not
startable (blocked, waiting on a real ``blocked_by`` link, closed or unknown);
a skipped queued issue keeps its place and starts once it becomes startable.
Issues not in the queue start after the queued ones, by issue number.

A cross-lane priority list, kept under ``<state>/global-queue.json``, ranks
listed issues ahead of unlisted ones with the same file-overlap count when a
tick orders every lane's candidates together.
"""

from __future__ import annotations

import json
from pathlib import Path

from foragent import orch_plan
from foragent.state import StateStore


def get(store: StateStore, lane: str) -> list[int]:
    return orch_plan.queue(store, lane)


def set_order(store: StateStore, lane: str, numbers: list[int], by: str = "human") -> list[int]:
    """Replace the lane's queue; an empty list clears it. Raises PlanRefused for a person's queue."""
    return orch_plan.set_queue(store, lane, numbers, by)


def ordered(store: StateStore, lane: str, numbers: list[int]) -> list[int]:
    """Queued numbers first in queue order, then the rest by number."""
    present = set(numbers)
    queued = [n for n in get(store, lane) if n in present]
    rest = sorted(present - set(queued))
    return queued + rest


def _global_path(store: StateStore) -> Path:
    return store.root / "global-queue.json"


def get_global(store: StateStore) -> list[int]:
    """The cross-lane priority list; a preference only, never a start rule."""
    path = _global_path(store)
    if not path.is_file():
        return []
    return [int(n) for n in json.loads(path.read_text(encoding="utf-8"))]


def set_global(store: StateStore, numbers: list[int]) -> list[int]:
    """Replace the cross-lane priority list; an empty list removes it."""
    order = list(dict.fromkeys(int(n) for n in numbers))
    with store._state_lock():
        try:
            before = get_global(store)
        except (OSError, TypeError, ValueError):
            before = None  # An unreadable list is replaced as before, and that is a change.
        if order:
            store._write_json(_global_path(store), order)
        else:
            _global_path(store).unlink(missing_ok=True)
        if order != before:
            store.record_flow("priority", issues=order)
    return order

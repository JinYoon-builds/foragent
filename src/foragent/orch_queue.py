"""A lane's preferred start order, kept locally under ``<state>/lane-queues.json``.

The queue is only a preference. The tick still skips issues that are not
startable (blocked, waiting on a real ``blocked_by`` link, closed or unknown);
a skipped queued issue keeps its place and starts once it becomes startable.
Issues not in the queue start after the queued ones, by issue number.
"""

from __future__ import annotations

import json
from pathlib import Path

from foragent.state import StateStore


def _path(store: StateStore) -> Path:
    return store.root / "lane-queues.json"


def _load(store: StateStore) -> dict[str, list[int]]:
    path = _path(store)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(lane): [int(n) for n in numbers] for lane, numbers in data.items()}


def get(store: StateStore, lane: str) -> list[int]:
    return _load(store).get(lane, [])


def set_order(store: StateStore, lane: str, numbers: list[int]) -> list[int]:
    """Replace the lane's queue; an empty list clears it."""
    order = list(dict.fromkeys(int(n) for n in numbers))
    with store._state_lock():
        queues = _load(store)
        if order:
            queues[lane] = order
        else:
            queues.pop(lane, None)
        store._write_json(_path(store), queues)
    return order


def ordered(store: StateStore, lane: str, numbers: list[int]) -> list[int]:
    """Queued numbers first in queue order, then the rest by number."""
    present = set(numbers)
    queued = [n for n in get(store, lane) if n in present]
    rest = sorted(present - set(queued))
    return queued + rest

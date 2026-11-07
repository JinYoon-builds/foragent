"""Places where Foragent work accumulates, each with its reader, trigger and alarm (#2332).

This module holds the shelf object's constructor and the places installed so far. The
``promises`` place (#2454) is the session promises in orch_promise; its numbers come from
that module, which is the place's only writer.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, kw_only=True)
class Place:
    name: str
    purpose: str
    reader: str
    why: str
    trigger: str
    channel: str
    alarm: dict
    capacity: dict
    removal: str
    person_load: dict

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None or value == "":
                raise ValueError(f"{field.name} is required")


PROMISES = Place(
    name="promises", purpose="Keep 'when X happens, do Y' promises until their owner does Y",
    reader="the promise's owner", why="Promises kept only in a conversation are missed",
    trigger="event happened (closed, merged, approved or time)",
    channel="owner's request box and next run prompt, then one level up, then /fa report line 2",
    alarm={"count": 1, "age": timedelta(minutes=60)}, capacity={"in": "promises recorded",
                                                                 "out": "promises marked done"},
    removal="fa orch promise done <id>", person_load={"cap": 0, "form": "multiple-choice"},
)
_PLACES = (PROMISES,)


def places() -> tuple[Place, ...]:
    """The installed places the gate asks each tick."""
    return _PLACES


def summary(store, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Place name -> {count, in_24h, out_24h, oldest_minutes} for `fa brief --json`."""
    from foragent import orch_promise

    row = orch_promise.shelf_row(store, now or store._now())
    return {PROMISES.name: {key: row[key] for key in ("count", "in_24h", "out_24h", "oldest_minutes")}}

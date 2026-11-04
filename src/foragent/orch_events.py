"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json
from typing import Any

from foragent.file_cache import invalidate, read_jsonl
from foragent.state import StateStore
from foragent.state_issues import _copy


def read_events(store: StateStore) -> list[dict]:
    """Shared read-only journal rows; I/O errors propagate to each reader's policy."""
    return read_jsonl(store.root / "orch-events.jsonl", tolerant=True, errors="replace")


def events(store: StateStore, kind: str) -> list[dict]:
    """Retained events of one kind in journal order, skipping unreadable records."""
    try:
        rows = read_events(store)
    except OSError:
        return []
    return [_copy(event) for event in rows if event.get("kind") == kind]


def append_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                 say: str | None = None, *, locked: bool = False, **fields: Any) -> dict:
    """Append one event; ``say`` is an optional line for a person, kept beside the raw summary.

    Extra ``fields`` (for example a review's verdict, or a plan edit's lane, values and
    author) follow the shared keys. ``locked`` means the caller already holds the
    (non-reentrant) state lock, so a state change and its event are written under one lock.
    """
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split()), **fields}
    if say is not None and " ".join(say.split()):
        event["say"] = " ".join(say.split())
    if locked:
        _write_line(store, event)
    else:
        with store._state_lock():
            _write_line(store, event)
    return event


def _write_line(store: StateStore, event: dict) -> None:
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        # A note's fields keep the text as given. Escape what str.splitlines would split on, so every
        # reader of the journal still sees one record per line.
        line = json.dumps(event, ensure_ascii=False)
        stream.write(line.replace("\x85", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + "\n")
    invalidate(store.root / "orch-events.jsonl")


def acknowledged_at(store: StateStore, n: int) -> datetime | None:
    """Newest recorded response to this issue, independent of the latest note."""
    times = []
    for event in read_events(store):
        if event.get("issue") == n and event.get("kind") == "acknowledged":
            times.append(datetime.fromisoformat(event["at"]))
    return max(times, default=None)


def has_event(store: StateStore, issue: int | None, kind: str, **fields: Any) -> bool:
    """Whether the journal already holds an event of ``kind`` for ``issue`` with these ``fields``."""
    for event in read_events(store):
        if event.get("issue") == issue and event.get("kind") == kind and all(
            event.get(key) == value for key, value in fields.items()
        ):
            return True
    return False

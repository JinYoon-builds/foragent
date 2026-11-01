"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json
from typing import Any

from foragent.state import StateStore


def events(store: StateStore, kind: str) -> list[dict]:
    """Retained events of one kind in journal order, skipping unreadable records."""
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == kind:
            result.append(event)
    return result


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
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def acknowledged_at(store: StateStore, n: int) -> datetime | None:
    """Newest recorded response to this issue, independent of the latest note."""
    path = store.root / "orch-events.jsonl"
    if not path.exists():
        return None
    times = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("issue") == n and event.get("kind") == "acknowledged":
            times.append(datetime.fromisoformat(event["at"]))
    return max(times, default=None)


def has_event(store: StateStore, issue: int | None, kind: str, **fields: Any) -> bool:
    """Whether the journal already holds an event of ``kind`` for ``issue`` with these ``fields``."""
    path = store.root / "orch-events.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("issue") == issue and event.get("kind") == kind and all(
            event.get(key) == value for key, value in fields.items()
        ):
            return True
    return False

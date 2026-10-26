"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json

from foragent.state import StateStore


def append_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                 say: str | None = None, details: dict | None = None, *, locked: bool = False) -> dict:
    """Append one event; ``say`` is an optional line for a person, kept beside the raw summary.

    ``details`` adds structured facts (e.g. a plan edit's lane, values and author).
    ``locked`` means the caller already holds the (non-reentrant) state lock, so a
    state change and its event are written under one lock.
    """
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split())}
    if say is not None and " ".join(say.split()):
        event["say"] = " ".join(say.split())
    for key, value in (details or {}).items():
        event.setdefault(key, value)
    if locked:
        _write_line(store, event)
    else:
        with store._state_lock():
            _write_line(store, event)
    return event


def _write_line(store: StateStore, event: dict) -> None:
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")

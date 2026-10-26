"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json
from typing import Any

from foragent.state import StateStore


def append_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                 say: str | None = None, **fields: Any) -> dict:
    """Append one event; ``say`` is an optional line for a person, kept beside the raw summary.

    Extra ``fields`` (for example a review's verdict) follow the shared keys.
    """
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split()), **fields}
    if say is not None and " ".join(say.split()):
        event["say"] = " ".join(say.split())
    with store._state_lock():
        _write(store, event)
    return event


def write_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                **fields: Any) -> dict:
    """Append an event with extra fields; the caller already holds the state lock."""
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split()), **fields}
    _write(store, event)
    return event


def _write(store: StateStore, event: dict) -> None:
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")

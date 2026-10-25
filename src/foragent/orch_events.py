"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json

from foragent.state import StateStore


def append_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                 say: str | None = None) -> dict:
    """Append one event; ``say`` is an optional line for a person, kept beside the raw summary."""
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split())}
    if say is not None and " ".join(say.split()):
        event["say"] = " ".join(say.split())
    with store._state_lock():
        with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event

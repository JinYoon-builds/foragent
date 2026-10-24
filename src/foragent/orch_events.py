"""Shared local journal for orchestrator actions and gate recovery."""

from datetime import UTC, datetime
import json
from typing import Any

from foragent.state import StateStore


def append_event(store: StateStore, at: datetime, issue: int | None, kind: str, summary: str,
                 **fields: Any) -> dict:
    """Append one event; extra fields (for example phase seconds) follow the shared keys."""
    event = {"at": at.astimezone(UTC).isoformat(), "issue": issue,
             "kind": kind, "summary": " ".join(summary.split()), **fields}
    with store._state_lock():
        with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event

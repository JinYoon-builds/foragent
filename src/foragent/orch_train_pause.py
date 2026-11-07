"""`fa orch pause-trains --why [--until]` and `fa orch resume-trains`: hold the merge owner's trains.

PM q73 (2026-11-07): each train merge moved main and the next train was built
before pending publications finished, so none landed for hours. A train pause
stops the merge owner from building a new train. The running train still
finishes, and publications and main-write requests are still applied.

Both commands append to `<state>/train-pause.jsonl`, the file's only writers.
`active()` derives the pause in force: the latest pause after the latest resume,
unless its optional end time has passed.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from foragent.state import StateStore


def _path(store: StateStore) -> Path:
    return store.root / "train-pause.jsonl"


def _records(store: StateStore) -> list[dict]:
    try:
        lines = _path(store).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue  # A torn line is no record.
        if isinstance(item, dict) and item.get("kind") in {"pause", "resume"}:
            records.append(item)
    return records


def _append(store: StateStore, record: dict) -> None:
    with store._state_lock(), _path(store).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def active(store: StateStore, now: datetime) -> dict | None:
    """The train pause in force as {"why", "by", "at", "until"}, or None."""
    current = None
    for item in _records(store):
        current = item if item["kind"] == "pause" else None
    if current is None:
        return None
    if until := current.get("until"):
        try:
            if now >= datetime.fromisoformat(until):
                return None
        except (TypeError, ValueError):
            pass  # An unreadable end time keeps the pause until a resume.
    return {key: current.get(key) for key in ("why", "by", "at", "until")}


def pause(store: StateStore, now: datetime, why: str, until: datetime | None = None, by: str = "") -> dict:
    record = {"kind": "pause", "at": now.isoformat(), "why": why, "by": by,
              "until": until.isoformat() if until else None}
    _append(store, record)
    return record


def resume(store: StateStore, now: datetime, by: str = "") -> bool:
    """Record a resume; True when a pause was in force."""
    was = active(store, now) is not None
    _append(store, {"kind": "resume", "at": now.isoformat(), "by": by})
    return was

"""The orchestrator's active Claude session, kept in ``orch-heartbeat.json`` (#413).

Writers of the session fields: every launch (gate, ``fa orch ensure`` and
``fa orch refresh --ready``) makes its new session active through
``activate``, inside the openers' lock. The heartbeat writer
(``StateStore.write_heartbeat``) only renews ``at`` and keeps these fields.
A replaced session moves to the heartbeat's ``previous`` field with status
``retired`` and is appended to ``orch-sessions.jsonl``, the full history.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from foragent.state import StateStore

DESCRIPTOR = ("session_id", "transcript_path", "started_at")
# Session fields of the heartbeat record; ``session`` is the watch's {pid, start_time}.
FIELDS = (*DESCRIPTOR, "status", "session")


def heartbeat_path(store: StateStore) -> Path:
    return store.root / "orch-heartbeat.json"


def read(store: StateStore) -> dict:
    """The orchestrator heartbeat record, or {} when it is missing or unreadable."""
    try:
        record = json.loads(heartbeat_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def active(store: StateStore) -> dict | None:
    """The active session's descriptor, or None when the heartbeat names none."""
    record = read(store)
    if not all(isinstance(record.get(key), str) and record[key] for key in DESCRIPTOR):
        return None
    try:
        if datetime.fromisoformat(record["started_at"]).tzinfo is None:
            return None
    except ValueError:
        return None
    return {key: record[key] for key in DESCRIPTOR}


def activate(store: StateStore, descriptor: dict, now: datetime, *, locked: bool = False) -> dict | None:
    """Make ``descriptor`` the active session; return the retired descriptor, if any.

    ``locked`` means the caller already holds the (non-reentrant) state lock.
    The heartbeat's ``at`` stays: the new session has not renewed it yet. The
    PID record of the old session goes with it.
    """
    if not locked:
        with store._state_lock():
            return activate(store, descriptor, now, locked=True)
    store.root.mkdir(parents=True, exist_ok=True)
    record = read(store)
    held = active(store)
    retired = None
    if held is not None and held["session_id"] != descriptor["session_id"]:
        retired = {**held, "status": "retired", "retired_at": now.isoformat()}
        with (store.root / "orch-sessions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(retired, ensure_ascii=False) + "\n")
    updated = {key: value for key, value in record.items() if key not in FIELDS}
    updated.update({key: descriptor[key] for key in DESCRIPTOR}, status="active")
    if retired is not None:
        updated["previous"] = retired
    store._write_json(heartbeat_path(store), updated)
    return retired

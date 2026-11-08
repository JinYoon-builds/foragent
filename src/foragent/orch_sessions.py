"""The orchestrator's active Claude session, kept in ``orch-heartbeat.json`` (#413).

Writers of the session fields: every launch (gate, ``fa orch ensure`` and
``fa orch refresh --ready``) makes its new session active through
``activate``, inside the openers' lock. The heartbeat writer
(``StateStore.write_heartbeat``) only renews ``at`` and keeps these fields.
A replaced session moves to the heartbeat's ``previous`` field with status
``retired`` and is appended to ``orch-sessions.jsonl``, the full history.
A running session that no launch recorded (Claude served the launch from a
pre-started process with its own session id, bug 11-09) makes itself active
through ``claim`` on its watch, unless a launch already retired it.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
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


def own_id() -> str | None:
    """The id of the session this process runs in.

    Claude exports its real session id (the transcript's name) as
    ``CLAUDE_CODE_SESSION_ID``. ``FORAGENT_SESSION_ID`` is the launch's id and
    can be inherited and stale in a pre-started process, so it is only the
    fallback outside Claude.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("FORAGENT_SESSION_ID") or None


def retired_ids(store: StateStore) -> set[str]:
    """Every session id a launch or refresh retired (``orch-sessions.jsonl``)."""
    try:
        lines = (store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    ids = set()
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("session_id"), str):
            ids.add(row["session_id"])
    return ids


def claim(store: StateStore, root: Path, session_id: str | None, now: datetime) -> dict | None:
    """Make the running session ``session_id`` active when no launch recorded it; the retired descriptor, if any.

    Nothing changes when the heartbeat names no session, when it already names
    this one, or when a launch or refresh retired this one: a replaced session
    never takes the record back.
    """
    if not session_id:
        return None
    with store._state_lock():
        held = active(store)
        if held is None or held["session_id"] == session_id or session_id in retired_ids(store):
            return None
        from foragent.orch_spawn_launch import transcript_path

        descriptor = {"session_id": session_id, "transcript_path": transcript_path(root.resolve(), session_id),
                      "started_at": now.isoformat()}
        return activate(store, descriptor, now, locked=True)

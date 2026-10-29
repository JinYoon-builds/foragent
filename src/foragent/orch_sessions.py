"""The orchestrator's active Claude session, kept in ``orch-heartbeat.json`` (#413).

Writers: a spawn (``orch_spawn``) and ``fa orch refresh --ready`` make a new
session active through ``activate``. The heartbeat writer
(``StateStore.write_heartbeat``) only renews ``at`` and keeps these fields.
A replaced session moves to the heartbeat's ``previous`` field with status
``retired`` and is appended to ``orch-sessions.jsonl``, the full history.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path

from foragent.state import StateStore

DESCRIPTOR = ("session_id", "transcript_path", "started_at")


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
    retired = None if record.get("session_id") == descriptor["session_id"] else _retire(store, record, now)
    updated = {key: value for key, value in record.items() if key not in (*DESCRIPTOR, "status", "session")}
    updated.update({key: descriptor[key] for key in DESCRIPTOR}, status="active")
    if retired is not None:
        updated["previous"] = retired
    store._write_json(heartbeat_path(store), updated)
    return retired


def _retire(store: StateStore, record: dict, now: datetime) -> dict | None:
    """Append the record's active descriptor to the history; return it as retired, or None."""
    old = {key: record[key] for key in DESCRIPTOR if key in record}
    if not old.get("session_id"):
        return None
    retired = {**old, "status": "retired", "retired_at": now.isoformat()}
    with (store.root / "orch-sessions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(retired, ensure_ascii=False) + "\n")
    return retired


def retire_active(store: StateStore, now: datetime, *, locked: bool = False) -> dict | None:
    """Leave no active session: an unnamed launch (``claude --bg``) claims the slot itself.

    The gate opens a session only when none is alive, so the recorded one is gone.
    """
    if not locked:
        with store._state_lock():
            return retire_active(store, now, locked=True)
    record = read(store)
    retired = _retire(store, record, now)
    if retired is None:
        return None
    updated = {key: value for key, value in record.items() if key not in (*DESCRIPTOR, "status", "session")}
    store._write_json(heartbeat_path(store), {**updated, "previous": retired})
    return retired


def claim(store: StateStore, descriptor: dict, now: datetime) -> bool:
    """Make ``descriptor`` active only when no session is active; whether it did.

    A session that was not named at launch (``claude --bg``, or one a person
    opened) records itself from ``fa orch watch``; a named or refreshed session
    already holds the slot, so a retired session never takes it back.
    """
    with store._state_lock():
        if active(store) is not None or descriptor["session_id"] in _retired_ids(store):
            return False
        activate(store, descriptor, now, locked=True)
        return True


def _retired_ids(store: StateStore) -> set[str]:
    try:
        lines = (store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    ids = set()
    for line in lines:
        try:
            ids.add(json.loads(line)["session_id"])
        except (ValueError, KeyError, TypeError):
            continue
    return ids


def own_id() -> str | None:
    """This process's Claude session id: the one Foragent named, else the one Claude Code exports."""
    return os.environ.get("FORAGENT_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def claim_own(store: StateStore, repo_root: Path, now: datetime) -> bool:
    """``fa orch watch`` at start: record this session when no session holds the slot."""
    session_id = own_id()
    if session_id is None:
        return False
    from foragent.orch_spawn_launch import transcript_path

    return claim(store, {"session_id": session_id, "transcript_path": transcript_path(repo_root.resolve(), session_id),
                         "started_at": now.isoformat()}, now)

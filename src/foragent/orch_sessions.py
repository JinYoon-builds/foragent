"""The orchestrator's active Claude session (#413).

Two records name a session: the heartbeat (``orch-heartbeat.json``) and the
spawn record (``orch-spawn.json``) of the last gate or ``fa orch ensure``
launch. The active session is derived from both: the later-started one.
Writers:
- a gate or ensure launch adds its descriptor to the spawn record only (the
  heartbeat stays byte-for-byte as the watch wrote it, a locked #128 contract);
- ``fa orch refresh --ready`` writes its new session into the heartbeat
  through ``activate``;
- the heartbeat writer (``StateStore.write_heartbeat``) renews ``at`` and, on
  the newer spawned session's first write, moves that descriptor into the
  heartbeat (``settle``).
A replaced session moves to the heartbeat's ``previous`` field with status
``retired`` and is appended to ``orch-sessions.jsonl``, the full history.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from foragent.state import StateStore

DESCRIPTOR = ("session_id", "transcript_path", "started_at")
# Session fields of the heartbeat record; ``session`` is the watch's {pid, start_time}.
FIELDS = (*DESCRIPTOR, "status", "session")


def heartbeat_path(store: StateStore) -> Path:
    return store.root / "orch-heartbeat.json"


def _read(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def read(store: StateStore) -> dict:
    """The orchestrator heartbeat record, or {} when it is missing or unreadable."""
    return _read(heartbeat_path(store))


def _descriptor(record: dict) -> dict | None:
    if not all(isinstance(record.get(key), str) and record[key] for key in DESCRIPTOR):
        return None
    try:
        if datetime.fromisoformat(record["started_at"]).tzinfo is None:
            return None
    except ValueError:
        return None
    return {key: record[key] for key in DESCRIPTOR}


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


def _spawned(store: StateStore) -> dict | None:
    """The last gate or ensure launch's session, unless it was retired since.

    Only ``record_spawn`` writes the descriptor, after its launch ran; later
    record writes carry it over, so a reservation or a failed launch keeps it.
    """
    descriptor = _read(store.root / "orch-spawn.json").get("descriptor")
    found = _descriptor(descriptor) if isinstance(descriptor, dict) else None
    return None if found is None or found["session_id"] in _retired_ids(store) else found


def _later(first: dict, second: dict | None) -> bool:
    """Whether ``first`` started no earlier; on a tie the spawn, the later launch, wins."""
    return second is None or datetime.fromisoformat(first["started_at"]) >= datetime.fromisoformat(second["started_at"])


def _replace(record: dict, descriptor: dict, now: datetime) -> tuple[dict, dict | None]:
    held = _descriptor(record)
    retired = None
    if held is not None and held["session_id"] != descriptor["session_id"]:
        retired = {**held, "status": "retired", "retired_at": now.isoformat()}
    updated = {key: value for key, value in record.items() if key not in FIELDS}
    updated.update(descriptor, status="active")
    if retired is not None:
        updated["previous"] = retired
    return updated, retired


def settle(store: StateStore, record: dict, now: datetime) -> tuple[dict, dict | None]:
    """``record`` with a newer spawned session made active; ``(record, retired)``. Writes nothing.

    A caller that writes the record holds the state lock and passes ``retired``
    to ``remember``.
    """
    spawned, held = _spawned(store), _descriptor(record)
    if spawned is None or (held is not None and held["session_id"] == spawned["session_id"]) \
            or not _later(spawned, held):
        return record, None
    return _replace(record, spawned, now)


def remember(store: StateStore, retired: dict | None) -> None:
    """Append one retired descriptor to ``orch-sessions.jsonl`` (caller holds the state lock)."""
    if retired is not None:
        with (store.root / "orch-sessions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(retired, ensure_ascii=False) + "\n")


def active(store: StateStore) -> dict | None:
    """The active session's descriptor (the heartbeat's, or a newer spawn's), or None. Reads only.

    Without a heartbeat file there is no active session to measure (#413):
    a spawned session counts once some orchestrator has written a heartbeat.
    """
    if not heartbeat_path(store).is_file():
        return None
    return _descriptor(settle(store, read(store), datetime.now(UTC))[0])


def activate(store: StateStore, descriptor: dict, now: datetime, *, locked: bool = False) -> dict | None:
    """Make ``descriptor`` the heartbeat's active session; return the retired descriptor, if any.

    ``locked`` means the caller already holds the (non-reentrant) state lock.
    The heartbeat's ``at`` stays: the new session has not renewed it yet. A
    newer spawned session is settled first, so it is retired too, not lost.
    """
    if not locked:
        with store._state_lock():
            return activate(store, descriptor, now, locked=True)
    store.root.mkdir(parents=True, exist_ok=True)
    record, superseded = settle(store, read(store), now)
    remember(store, superseded)
    updated, retired = _replace(record, descriptor, now)
    remember(store, retired)
    store._write_json(heartbeat_path(store), updated)
    return retired


def record_spawn(store: StateStore, descriptor: dict, now: datetime) -> None:
    """Make a gate or ensure launch's session the spawn record's one (inside the openers' lock).

    The session it replaces, if not retired yet, goes to the history as
    retired, so a later launch never drops it. The heartbeat is not written.
    """
    path = store.root / "orch-spawn.json"
    with store._state_lock():
        outgoing, held = _spawned(store), _descriptor(read(store))
        # A session already in the heartbeat is retired when the next one settles there.
        if outgoing is not None and outgoing["session_id"] not in (descriptor["session_id"],
                                                                   held and held["session_id"]):
            remember(store, {**outgoing, "status": "retired", "retired_at": now.isoformat()})
        record = _read(path)
        if record.get("launched") is True:
            store._write_json(path, {**record, "descriptor": descriptor})

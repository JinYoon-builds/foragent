"""One orchestrator seat per project: the lease in ``<state>/orch-seat.json`` (#962).

The lease names the seat holder by its process lifetime ``(pid, start_time)``:
``{"holder": {"pid", "start_time", "taken_at", "session_id"?}, "previous": old?}``.
``taken_at`` is when that lifetime took the seat. Writers: a launch (gate,
``fa orch ensure``, ``fa orch refresh --ready``) inside the openers' lock, and
``fa orch watch --follow`` when it takes a free seat. Liveness comes only from
``processes.recorded_identity_evidence``; an unreadable identity is unknown,
never proof that the holder is gone. A corrupt lease counts as absent.
"""

from datetime import datetime
import json
import os
from pathlib import Path

from foragent import processes
from foragent.state import StateStore


class SeatHeld(Exception):
    """Another live (or unknown) lifetime holds the seat."""


def path(store: StateStore) -> Path:
    return store.root / "orch-seat.json"


def _valid_holder(holder) -> bool:
    return (isinstance(holder, dict) and isinstance(holder.get("pid"), int)
            and not isinstance(holder.get("pid"), bool)
            and isinstance(holder.get("start_time"), (int, float)) and isinstance(holder.get("taken_at"), str))


def read(store: StateStore) -> tuple[dict | None, str | None]:
    """``(lease, reason)``: the lease, or None with ``"corrupt"`` when it cannot be read."""
    try:
        lease = json.loads(path(store).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, "corrupt"
    if not isinstance(lease, dict) or not _valid_holder(lease.get("holder")):
        return None, "corrupt"
    return lease, None


def alive(holder: dict) -> bool | None:
    """Whether the recorded lifetime runs: True, False (dead or reused) or None (unreadable)."""
    return processes.recorded_identity_evidence(holder["pid"], holder["start_time"], fresh=True).alive


def blocks_spawn(store: StateStore) -> bool:
    """A live or unknown lease holder suppresses a spawn; absent, corrupt, dead or reused does not."""
    lease, _reason = read(store)
    return lease is not None and alive(lease["holder"]) is not False


def report(store: StateStore) -> dict:
    """``fa orch seat --json``: read only; never takes the seat."""
    lease, reason = read(store)
    if lease is None:
        result = {"holder": None, "alive": False}
        if reason:
            result["reason"] = reason
        return result
    state = alive(lease["holder"])
    result = {"holder": lease["holder"], "alive": state}
    if state is None:
        result["reason"] = "unknown"
    if "previous" in lease:
        result["previous"] = lease["previous"]
    return result


def _write(store: StateStore, holder: dict, old: dict | None) -> None:
    """Write ``holder``; the outgoing holder, when there was one, becomes ``previous``."""
    lease = {"holder": holder}
    if old is not None:
        lease["previous"] = old["holder"]
    store._write_json(path(store), lease)


def hand_over(store: StateStore, pid, start_time, session_id: str | None, now: datetime) -> bool:
    """A launch took the seat (caller holds the openers' lock); False when the identity is unknown."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(start_time, (int, float)):
        return False
    holder = {"pid": pid, "start_time": start_time, "taken_at": now.isoformat()}
    if session_id:
        holder["session_id"] = session_id
    with store._state_lock():
        _write(store, holder, read(store)[0])
    return True


def _me() -> dict | None:
    """This watch's identity: the nearest Claude session, else this process itself."""
    session = processes.claude_session()
    if session is not None:
        return session
    try:
        row = processes.read_process_identity(os.getpid())
        if row is not None:
            return {"pid": row.pid, "start_time": row.start_time}
    except Exception:  # noqa: BLE001 - fall through to the direct read
        pass
    try:  # This process can read its own start time even when listings are unavailable.
        return {"pid": os.getpid(), "start_time": processes.psutil.Process(os.getpid()).create_time()}
    except Exception:  # noqa: BLE001 - an unreadable self is no identity
        return None


def _ancestor(holder: dict) -> bool:
    """The holder is an ancestor of this process: the launcher that opened this session."""
    try:
        current = processes.psutil.Process(os.getpid()).parent()
        while current is not None:
            if current.pid == holder["pid"] and current.create_time() == holder["start_time"]:
                return True
            current = current.parent()
    except (processes.psutil.Error, OSError, SystemError):
        return False
    return False


def _same(holder: dict, me: dict) -> bool:
    return (holder["pid"], holder["start_time"]) == (me["pid"], me["start_time"])


def take_for_watch(store: StateStore, now: datetime) -> dict:
    """Take or resume the seat for ``fa orch watch --follow``; return the holder or raise SeatHeld.

    A free (absent, corrupt, dead or reused) seat, or one held by this
    session's own launcher, is taken; the same lifetime resumes with its
    ``taken_at`` and gains a newly known ``session_id``. A different live or
    unknown holder refuses, and so does a watch whose own identity cannot be
    read: it could never be told apart from another session. The openers'
    lock serializes this with launches and refresh handovers.
    """
    from foragent import watch_open

    me = _me()
    session_id = os.environ.get("FORAGENT_SESSION_ID")
    with watch_open._opening(store):
        if me is None:
            raise SeatHeld("this session's identity cannot be read")
        lease, _reason = read(store)
        if lease is not None:
            holder = lease["holder"]
            if _same(holder, me):
                if session_id and holder.get("session_id") != session_id:
                    holder = {**holder, "session_id": session_id}  # Enrich; taken_at stays.
                    with store._state_lock():
                        store._write_json(path(store), {**lease, "holder": holder})
                return holder
            state = alive(holder)
            if state is None or (state and not _ancestor(holder)):
                raise SeatHeld("another orchestrator holds the seat")
            if state:  # This session's launcher: the session replaces it, keeping its previous.
                lease = {"holder": lease["previous"]} if "previous" in lease else None
        holder = {"pid": me["pid"], "start_time": me["start_time"], "taken_at": now.isoformat()}
        if session_id:
            holder["session_id"] = session_id
        with store._state_lock():
            _write(store, holder, lease)
        return holder


def still_holds(store: StateStore, holder: dict) -> bool:
    """Whether this watch's lifetime still holds the seat; a vanished or corrupt lease is a free seat."""
    lease, _reason = read(store)
    return lease is None or _same(lease["holder"], holder)


def beat(store: StateStore, holder: dict, write) -> None:
    """Check the seat and run ``write`` (the heartbeat) as one step under the openers' lock.

    A refresh hands the seat over under the same lock, so no heartbeat from
    the outgoing holder can land after the handover. Raises SeatHeld instead.
    A vanished or corrupt lease is a free seat: the holder writes itself back
    (same ``taken_at``) before its heartbeat, so the watch never runs unrecorded.
    """
    from foragent import watch_open

    with watch_open._opening(store):
        lease, _reason = read(store)
        if lease is None:
            with store._state_lock():
                _write(store, holder, None)
        elif not _same(lease["holder"], holder):
            raise SeatHeld("another orchestrator holds the seat")
        write()

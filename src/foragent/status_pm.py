"""One PM seat per person and project: the authority contract (#1484).

The seat lives in ``<state>/pm-seat.json`` of the project's StateStore, so it is
scoped to one repository and, through the person's home, to one person. Inside
the file each person has their own entry, keyed by login name, and a claim only
ever replaces the claiming person's entry:

    {"people": {"<person>": {"holder": {"session_id", "taken_at", "root"},
                             "displaced": [{"session_id", "moved_at"}, ...]}}}

Writer: only ``claim``, which bare ``fa`` calls from the session that typed it
(the latest explicit entrance wins). Hooks, heartbeats and the statusline only
read. A session is identified by ``FORAGENT_SESSION_ID`` (what ``claim`` records)
or, in a Claude Code hook without it, by the hook's ``session_id``. The person is
this process's account, read from its user id.

Dependent features ask ``authority(store, session_id)``:

- ``"holder"``: this session is the PM.
- ``"displaced"``: this session held the seat and another entrance took it. It
  must not publish or approve; it keeps its remaining notes in FA and steps back.
- ``"none"``: no seat is held by this person, or this session never held it.
  Nothing changes for such callers (scripts, the gate, the orchestrator).
- ``"unknown"``: the record exists but cannot be read; publication refuses.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import pwd

from foragent.state import StateStore


class SeatMoved(Exception):
    """This session's PM seat moved to another terminal."""

    def __init__(self, holder: dict):
        super().__init__(holder.get("session_id", ""))
        self.holder = holder


class SeatUnreadable(Exception):
    """The seat record exists but cannot be read; nobody's authority can be told."""


def person() -> str:
    """The login of this process's account, from its user id, never from changeable env."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return f"uid-{os.getuid()}"


def path(store: StateStore) -> Path:
    return store.root / "pm-seat.json"


def _valid_seat(seat) -> bool:
    if not isinstance(seat, dict) or not isinstance(seat.get("holder"), dict):
        return False
    holder_id = seat["holder"].get("session_id")
    rows = seat.get("displaced", [])
    return (isinstance(holder_id, str) and bool(holder_id) and isinstance(rows, list)
            and all(isinstance(row, dict) and isinstance(row.get("session_id"), str) for row in rows))


def _read_all(store: StateStore) -> dict:
    """The whole seat file; a missing file has no seats. Raise SeatUnreadable otherwise.

    An unreadable file or entry is never read as "no seat": that would forget every
    displaced session and let it publish again.
    """
    try:
        data = json.loads(path(store).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"people": {}}
    except (OSError, ValueError) as exc:
        raise SeatUnreadable(f"{path(store)}: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("people"), dict):
        raise SeatUnreadable(f"{path(store)}: not a seat record")
    if not all(_valid_seat(seat) for seat in data["people"].values()):
        raise SeatUnreadable(f"{path(store)}: a seat entry is malformed")
    return data


def read(store: StateStore, who: str | None = None) -> dict | None:
    """This person's seat ``{"holder", "displaced"}``, None when none was claimed.

    Raises SeatUnreadable when the record exists but cannot be read.
    """
    seat = _read_all(store)["people"].get(who or person())
    if seat is None:
        return None
    return {"holder": seat["holder"], "displaced": list(seat.get("displaced", []))}


def claim(store: StateStore, session_id: str, now: datetime, root: Path, who: str | None = None) -> dict:
    """Make ``session_id`` this person's PM; return ``{"holder", "previous", "kept"}``.

    ``kept`` is True when this session already held the seat. The outgoing holder
    joins ``displaced`` for good (every displaced session stays refused until it
    enters again); a session that enters again leaves it. Other people's entries
    are written back unchanged. An unreadable record raises SeatUnreadable and is
    left as it is, so its displaced sessions are not forgotten by an overwrite.
    """
    if not session_id:
        raise ValueError("a PM claim needs a session id")
    who = who or person()
    store.root.mkdir(parents=True, exist_ok=True)
    with store._state_lock():
        data = _read_all(store)
        seat = data["people"].get(who)
        old = seat["holder"] if seat else None
        displaced = [row for row in (seat.get("displaced", []) if seat else []) if row["session_id"] != session_id]
        previous = None
        if old is not None and old["session_id"] != session_id:
            previous = old
            displaced.append({"session_id": old["session_id"], "moved_at": now.isoformat()})
        holder = ({**old, "root": str(root)} if previous is None and old is not None
                  else {"session_id": session_id, "taken_at": now.isoformat(), "root": str(root)})
        data["people"][who] = {"holder": holder, "displaced": displaced}
        store._write_json(path(store), data)
    return {"holder": holder, "previous": previous, "kept": old is not None and previous is None}


def authority(store: StateStore, session_id: str | None, who: str | None = None) -> str:
    """``"holder"``, ``"displaced"``, ``"none"`` or ``"unknown"`` for ``session_id``.

    ``"unknown"``: the session has an id but the record cannot be read, so it may
    be displaced. A caller without an id never claimed a seat and stays ``"none"``.
    """
    if not session_id:
        return "none"
    try:
        seat = read(store, who)
    except SeatUnreadable:
        return "unknown"
    if seat is None:
        return "none"
    if seat["holder"]["session_id"] == session_id:
        return "holder"
    if any(row["session_id"] == session_id for row in seat["displaced"]):
        return "displaced"
    return "none"


def require_not_displaced(store: StateStore, session_id: str | None, who: str | None = None) -> None:
    """Raise SeatMoved when ``session_id`` lost this person's seat, SeatUnreadable when that
    cannot be told; otherwise do nothing."""
    state = authority(store, session_id, who)
    if state == "unknown":
        read(store, who)  # Raises SeatUnreadable with the reason.
    if state == "displaced":
        raise SeatMoved(read(store, who)["holder"])


def short(session_id: str) -> str:
    """A session id short enough for a statusline; long ids keep their first eight characters."""
    return session_id if len(session_id) <= 12 else session_id[:8] + "…"


def session_from_env() -> str | None:
    return os.environ.get("FORAGENT_SESSION_ID") or None

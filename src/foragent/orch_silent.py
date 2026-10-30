"""One push when the seat's live holder stays silent past ``orch_silent_alert_minutes`` (#952).

The gate and ``fa orch ensure`` decide from liveness facts that no second
orchestrator may open. This module only times the silence of the seat holder
(``orch_seat``, #962) and reaches the person once per silence episode. It never
reads what the session is doing, never writes its heartbeat and never takes the
seat. Other liveness evidence (a recorded heartbeat session, an argv match, the
spawn record) may suppress a spawn, but it names no holder, so it opens no
episode here.

An episode belongs to one holder lifetime ``(pid, start_time)``. Its silence
starts at the later of the heartbeat's ``at`` and the holder's ``taken_at``.
``orch-silent-alert.json`` records the holder already pushed; it applies only
while that holder still holds the seat. A fresh heartbeat ends the episode
(the record is deleted), whether the caller or the read under this module's
lock sees it. Every read and write of the record, including the push between
them, runs under ``orch-silent-alert.lock``, so the gate and ``fa orch ensure``
never both push one episode and a rearm never lands between a push and its record.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path

from foragent import notify, orch_seat
from foragent.commands.orch import ALIVE_SECONDS  # The liveness limit orch_spawn._alive uses.
from foragent.config import Config
from foragent.i18n import text
from foragent.state import StateStore


def _path(store: StateStore) -> Path:
    return store.root / "orch-silent-alert.json"


@contextmanager
def _locked(store: StateStore):
    """The one lock for the record; held across the push so concurrent checks see its result."""
    store.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(store.root / "orch-silent-alert.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def rearm(store: StateStore) -> None:
    """A fresh heartbeat ends the silence episode; the next silence may push again."""
    with _locked(store):
        _path(store).unlink(missing_ok=True)


def _at(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def _fresh(store: StateStore, now: datetime) -> bool:
    """The same freshness test as ``orch_spawn._alive``, read again under this module's lock."""
    last = store.heartbeats()["orchestrator"]
    return last is not None and (now - last).total_seconds() <= ALIVE_SECONDS


def _pushed_holder(store: StateStore):
    try:
        record = json.loads(_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record.get("holder") if isinstance(record, dict) else None


def check(root: Path, store: StateStore, config: Config, now: datetime) -> bool:
    """Push once when the seat's live holder has been silent past the threshold; return whether it pushed.

    A holder whose liveness cannot be read counts as live; a dead or reused
    holder, an absent or corrupt seat and an unreadable ``taken_at`` with no
    heartbeat push nothing.
    """
    minutes = config.orch_silent_alert_minutes
    if minutes <= 0:
        return False
    with _locked(store):
        if _fresh(store, now):
            _path(store).unlink(missing_ok=True)
            return False
        lease, _reason = orch_seat.read(store)
        if lease is None:
            return False
        holder = lease["holder"]
        identity = [holder["pid"], holder["start_time"]]
        if _pushed_holder(store) == identity or orch_seat.alive(holder) is False:
            return False
        moments = [store.heartbeats()["orchestrator"], _at(holder["taken_at"])]
        known = [moment for moment in moments if moment is not None]
        if not known or now - max(known) < timedelta(minutes=minutes):
            return False
        language = config.language
        try:
            with notify.sending("silence", root):
                sent = notify.send(config, text("orch_silent_title", language),
                                   text("orch_silent_alert", language, directory=root.name))
        except Exception:  # noqa: BLE001 - a failed push is retried on the next check
            sent = False
        if sent is not True:
            return False
        store._write_json(_path(store), {"holder": identity, "pushed_at": now.isoformat(),
                                         "origin": max(known).isoformat()})
    return True

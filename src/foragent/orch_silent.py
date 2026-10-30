"""One push when a live orchestrator stays silent past ``orch_silent_alert_minutes`` (#952).

The gate and ``fa orch ensure`` already decide, from liveness facts, that an
orchestrator session is running without a fresh heartbeat. This module only
times that silence and reaches the person once per silence episode. It never
reads what the session is doing and never writes its heartbeat.

Silence starts at the latest known fact among the heartbeat's ``at``, the spawn
record's ``spawned_at`` and the start times of the live orchestrator processes.

``orch-silent-alert.json`` is the open episode: every session identity seen
in it (``seen``) and when it was pushed (``pushed_at``). A fresh heartbeat
ends the episode (the file is deleted), whether the caller or this module's
own read under the lock sees it. An identity not in ``seen`` (a new spawn, a
changed recorded session, another live orchestrator process) is a new
session and starts a new episode; an identity merely missing from one read
(a failed scan, an unreadable spawn record) changes nothing. Every read and
write of the file, including the push between them, runs under
``orch-silent-alert.lock``, so the gate and ``fa orch ensure`` never both push
one episode and a rearm never lands between a push and its record.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path

from foragent import notify, processes
from foragent.commands.orch import ALIVE_SECONDS  # The liveness limit orch_spawn._alive uses.
from foragent.config import Config
from foragent.i18n import text
from foragent.state import StateStore

SKILL = "/fa-orchestrator"


def _path(store: StateStore) -> Path:
    return store.root / "orch-silent-alert.json"


@contextmanager
def _locked(store: StateStore):
    """The one lock for this file; held across the push so concurrent checks see its record."""
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


def _spawn_record(store: StateStore) -> dict:
    try:
        record = json.loads((store.root / "orch-spawn.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _at(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def facts(store: StateStore, root: Path) -> tuple[datetime | None, set[str]]:
    """The silence origin (None when no fact gives one) and the session identities seen now.

    Identities are the spawn record's ``spawned_at`` and the ``(PID, start
    time)`` of the recorded heartbeat session (when alive) and of every live
    orchestrator process the argv scan finds.
    """
    spawned_at = _spawn_record(store).get("spawned_at")
    spawned = _at(spawned_at)
    rows = list(processes.orchestrator_process_starts(root, SKILL))
    session = store.heartbeat_session("orchestrator")
    if session is not None and processes.recorded_identity_evidence(
            session["pid"], session["start_time"], fresh=True).alive:
        rows.append((session["pid"], session["start_time"]))
    identities = {f"process:{pid}:{start!r}" for pid, start in rows}
    if spawned is not None:
        identities.add(f"spawn:{spawned_at}")
    moments = [store.heartbeats()["orchestrator"], spawned,
               *(datetime.fromtimestamp(start, UTC) for _, start in rows)]
    known = [moment for moment in moments if moment is not None]
    return (max(known) if known else None), identities


def _episode(store: StateStore) -> dict:
    try:
        episode = json.loads(_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return episode if isinstance(episode, dict) else {}


def _fresh(store: StateStore, now: datetime) -> bool:
    """The same freshness test as ``orch_spawn._alive``, read again under this module's lock."""
    last = store.heartbeats()["orchestrator"]
    return last is not None and (now - last).total_seconds() <= ALIVE_SECONDS


def check(root: Path, store: StateStore, config: Config, now: datetime) -> bool:
    """Push once when the live, heartbeat-less orchestrator passed the threshold; return whether it pushed.

    Under the lock the heartbeat is read once more: a heartbeat renewed since
    the caller's liveness check is fresh here too, so it rearms instead of
    being measured as silence. An identity not seen before in this episode is
    a new session and starts a new episode; one that is merely not seen now
    (a failed scan, an unreadable spawn record) changes nothing.
    """
    minutes = config.orch_silent_alert_minutes
    if minutes <= 0:
        return False
    with _locked(store):
        if _fresh(store, now):
            _path(store).unlink(missing_ok=True)
            return False
        origin, identities = facts(store, root)
        episode = _episode(store)
        seen = set(episode.get("seen") or ())
        pushed_at = episode.get("pushed_at")
        if pushed_at is not None and identities - seen:
            pushed_at = None  # A new session: the pushed episode is over.
        record = {"seen": sorted(seen | identities), "pushed_at": pushed_at}
        if pushed_at is None and origin is not None and now - origin >= timedelta(minutes=minutes):
            language = config.language
            try:
                sent = notify.send(config, text("orch_silent_title", language),
                                   text("orch_silent_alert", language, directory=root.name))
            except Exception:  # noqa: BLE001 - a failed push is retried on the next check
                sent = False
            if sent is True:
                record["pushed_at"] = now.isoformat()
        if record != episode:
            store._write_json(_path(store), record)
    return record["pushed_at"] is not None and record["pushed_at"] != pushed_at

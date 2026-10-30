"""`fa orch ensure`: the planning session's explicit orchestrator spawn (D76).

The gate's spawn in ``orch_spawn.ensure_orchestrator`` is unchanged; that
function hands ``spacing=False`` calls to ``ensure_explicit`` here. It skips
the two-minute spacing but never opens a second orchestrator: every guard is
checked again inside the openers' lock, and a spawn that another opener made
while this call waited, or was making when it began, stops it.
"""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path

from foragent import orch_spawn
from foragent.config import Config
from foragent.state import StateStore


def _last_spawn(store: StateStore) -> dict | None:
    """The spawn record as stored, or None when there is none or it cannot be read."""
    try:
        record = json.loads(orch_spawn._record_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def skip_reason(repo_root: Path, store: StateStore, now: datetime) -> str | None:
    """Why no orchestrator should open now, as a word, or None when one may open (spacing aside)."""
    if os.environ.get("FORAGENT_ORCH_SPAWN") == "off":
        return "off"
    if orch_spawn._alive(store, now):
        return "alive"
    if not orch_spawn._approved_work(store):
        return "no_work"
    if orch_spawn._session_alive(store, repo_root.resolve()):
        return "session"
    return None


@contextmanager
def _explicit_lock(store: StateStore):
    """Take the openers' lock (the same file as ``watch_open._opening``); yield whether it was busy.

    Busy means another opener was mid-open when this call began, so a spawn
    record it wrote before this call read the record is still a fresh spawn.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(store.root / "watch-open.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = False
        except BlockingIOError:
            fcntl.flock(fd, fcntl.LOCK_EX)
            busy = True
        yield busy
    finally:
        os.close(fd)


def ensure_explicit(repo_root: Path, store: StateStore, config: Config, now: datetime,
                    why: list | None) -> str | None:
    """``fa orch ensure``: the gate's spawn without its spacing, never a second orchestrator.

    The gate's own path is unchanged. Here every guard is checked again inside
    the lock, and a spawn that another opener made while this call waited (the
    record changed) or was making when it began (lock busy and a spawn within
    the spacing) stops this call. Each stop appends its reason word to ``why``.
    """
    why = [] if why is None else why
    seen = _last_spawn(store)
    reason = skip_reason(repo_root, store, now)
    if reason is not None:
        if reason == "session":
            orch_spawn._note_silent(store, now)
        why.append(reason)
        return None
    root = repo_root.resolve()
    with _explicit_lock(store) as busy:
        if (reason := skip_reason(root, store, now)) is not None:
            why.append(reason)
            return None
        if _last_spawn(store) != seen or (busy and orch_spawn._spaced(store, now)):
            why.append("raced")
            return None
        planned = orch_spawn.plan_launch(root, store, now)
        # The same record as the gate writes, so the gate's spacing also covers this spawn.
        if planned is None or not orch_spawn.launch(root, store, now, planned):
            why.append("failed")
            return None
        orch_spawn.adopt(store, planned, now)
    orch_spawn._opened(store, config, root, now, planned)
    return planned[0]

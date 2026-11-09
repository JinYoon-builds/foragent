"""Process-lifetime intents, with file locks only around their local transitions.

The token excludes concurrent threads as well as processes. A stopped process's
intent can be reclaimed; an unreadable process identity never proves it stopped.
Work runs after the lock is released, and completion removes only its own token.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

import psutil

from foragent.file_locks import LockDeadline, LockTimeout, check_order, cleanup, file_lock, ordered, owner, reporting


_SECTIONS = threading.local()


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _child_path(path, token):
    return path.with_name(f".{path.name}.{token}.child")


def _release_path(path, token):
    return path.with_name(f".{path.name}.{token}.released")


def _read(path):
    row = _json(path)
    if row is not None:
        # The parent records its child independently of the adoption lock. A
        # late handoff cannot overwrite an adopted or replaced intent.
        child = _json(_child_path(path, row["token"]))
        if child is not None:
            row = {**row, **child}
        released = _json(_release_path(path, row["token"]))
        if released is not None and released["pid"] == row["pid"]:
            return None
    return row


def _remove_receipts(path, token):
    _child_path(path, token).unlink(missing_ok=True)
    _release_path(path, token).unlink(missing_ok=True)


def _read_identity(pid):
    try:
        process = psutil.Process(pid)
        return None if process.status() == psutil.STATUS_ZOMBIE else process.create_time()
    except psutil.NoSuchProcess:
        return None


# A process's own birth time never changes, so it is read once per process (a
# fork has a new pid and reads its own), not again on every section acquire.
_SELF = {}


def _identity(pid):
    if pid != os.getpid():
        return _read_identity(pid)
    if pid not in _SELF:
        _SELF.clear()
        _SELF[pid] = _read_identity(pid)
    return _SELF[pid]


_identity(os.getpid())


def _alive(row):
    if row is None:
        return False
    try:
        started = _identity(row["pid"])
        return started is not None and started == row["start_time"]
    except (psutil.AccessDenied, OSError):
        return True


def _write(path, row):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def acquire(path: Path):
    """Claim an idle intent without waiting; return its token or None.

    Probe the previous process outside the lock, then compare the snapshot under
    it. The lock path also excludes an older process still using a kernel hold.
    """
    path = Path(path)
    me = {**owner(), "start_time": _identity(os.getpid()), "token": uuid4().hex}
    while True:
        previous = _read(path)
        if _alive(previous):
            return None
        try:
            with file_lock(path.with_suffix(".lock"), timeout=0):
                if _read(path) != previous:
                    continue
                replaced = _json(path)
                _write(path, me)
                if replaced is not None:
                    _remove_receipts(path, replaced["token"])
                return me["token"]
        except LockTimeout:
            return None


def release(path: Path, token, *, pid=None):
    # A launcher's failure cleanup can run under tick.lock. It must neither
    # wait for adoption nor release a child that already owns the reservation.
    row = _read(path)
    if row is None or row["token"] != token or pid is not None and row["pid"] != pid:
        return
    try:
        with cleanup(), file_lock(path.with_suffix(".lock"), timeout=0 if pid is not None else None):
            row = _read(path)
            if row is not None and row["token"] == token and (pid is None or row["pid"] == pid):
                path.unlink()
                _remove_receipts(path, token)
    except LockTimeout:
        if pid is None:
            raise
        # A failed launcher must not strand an intent owned by its still-live
        # scheduler. This cancellation applies only to that token AND pid;
        # concurrent child adoption remains valid and a new token ignores it.
        receipt = _release_path(path, token)
        _write(receipt, {"pid": pid})
        current = _json(path)
        if current is None or current["token"] != token:
            receipt.unlink(missing_ok=True)


def handoff(path: Path, token, pid):
    """Record the child's lifetime without acquiring its adoption lock.

    The token-specific receipt makes an unadopted, stopped child reclaimable
    even while the launcher stays alive. Adoption and release only touch the
    main intent; a late receipt for a retired token cannot resurrect it.
    """
    started = _identity(pid)
    row = _json(path)
    if row is not None and row["token"] == token and row["pid"] == os.getpid():
        receipt = _child_path(path, token)
        _write(receipt, {"pid": pid, "start_time": started})
        current = _json(path)
        if current is None or current["token"] != token:
            receipt.unlink(missing_ok=True)


@contextmanager
def adopted(path: Path, token):
    """Adopt a launch token before doing work; a replaced token starts nothing."""
    me = {"pid": os.getpid(), "start_time": _identity(os.getpid()), "token": token}
    with file_lock(path.with_suffix(".lock")):
        row = _read(path)
        owned = row is not None and row["token"] == token
        if owned:
            _write(path, me)
    try:
        yield owned
    finally:
        if owned:
            release(path, token)


@contextmanager
def process_owner(path: Path, reservation):
    """Use a launch token, or release an inherited legacy fd before claiming.

    Older callers supply a duplicated descriptor. Convert that transport at
    entry, before constructing the worker or doing any external work.
    """
    if isinstance(reservation, str):
        row = _read(path)
        if row is None or row["token"] != reservation:
            try:
                reservation = int(reservation)
            except ValueError:
                pass
    if type(reservation) is int:
        with os.fdopen(reservation, "a") as inherited:
            fcntl.flock(inherited, fcntl.LOCK_UN)
        with hold(path) as owned:
            yield owned
    else:
        with adopted(path, reservation) as owned:
            yield owned


@contextmanager
def hold(path: Path):
    token = acquire(path)
    try:
        yield token is not None
    finally:
        if token is not None:
            release(path, token)


@contextmanager
def section(path: Path, *, timeout=None, journal_root=None, reentrant=False):
    """Exclude a section using a process token, flocking only its local transitions.

    ``path`` remains the transition lock (including for legacy kernel holders).
    Its sibling .intent records the process birth identity and unique token.
    Polling waits without flock; ordering spans the work, and optional nesting
    reuses only this thread's hold, never a different thread's or a fork's.
    An immediate probe yields False; a bounded wait raises LockTimeout.
    """
    path = Path(path)
    key = (os.getpid(), str(path.resolve()))
    held = _SECTIONS.__dict__.setdefault("held", set())
    if reentrant and key in held:
        yield True
        return
    check_order(path)
    intent = path.with_suffix(".intent")
    with reporting(journal_root):
        deadline = LockDeadline(path, timeout, journal_root)
        while (token := acquire(intent)) is None:
            if timeout == 0:
                yield False
                return
            deadline.pause()
        held.add(key)
        try:
            with ordered(path):
                yield True
        finally:
            held.discard(key)
            # Ordering is unwound before the short transition lock is retaken.
            # Release is permitted even when work timed out at another lock.
            # A fork unwinding this copied scope cannot release its parent's token.
            if os.getpid() == key[0]:
                release(intent, token)

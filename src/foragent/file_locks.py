"""Bounded kernel locks and their ownership evidence.

Ownership locks (tick, merge-owner, prepare/flush) precede route/merge/main-write
locks, then capacity.lock, repository state.lock files in sorted path order,
then leaf locks (sensor, flow, etc.). Never acquire capacity under state.
Nonblocking ownership probes do not wait. Inherited owner descriptors keep the
kernel hold; the child replaces the parent's ownership evidence on adoption.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import psutil

DEFAULT_TIMEOUT = 600.0
POLL_SECONDS = 0.1
_JOURNAL: ContextVar[Path | None] = ContextVar("lock_journal", default=None)
_HELD: ContextVar[tuple[tuple[int, str], ...]] = ContextVar("ordered_locks", default=())
_ATTEMPT: ContextVar[bool] = ContextVar("lock_attempt", default=False)


class LockTimeout(TimeoutError):
    """A lock stayed busy until its deadline; ownership is also in the journal."""


_ABORTED: ContextVar[LockTimeout | None] = ContextVar("lock_attempt_timeout", default=None)


@contextmanager
def reporting(root: Path | None = None) -> Iterator[None]:
    """One operation's journal and timeout scope; unwinding never starts another wait."""
    token = _JOURNAL.set(root or _JOURNAL.get())
    aborted = None if _ATTEMPT.get() else _ABORTED.set(None)
    attempt = _ATTEMPT.set(True)
    try:
        yield
    finally:
        _ATTEMPT.reset(attempt)
        if aborted is not None:
            _ABORTED.reset(aborted)
        _JOURNAL.reset(token)


def _give_up(path: Path, seconds: float, journal_root: Path | None) -> None:
    record_timeout(path, seconds, journal_root)
    error = LockTimeout(f"Timed out waiting for {path} after {seconds:g}s")
    if _ATTEMPT.get():
        _ABORTED.set(error)
    raise error


def wait_limit() -> float:
    value = float(os.environ.get("FORAGENT_LOCK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))
    if not math.isfinite(value) or value < 0:
        raise ValueError("FORAGENT_LOCK_TIMEOUT_SECONDS must be finite and nonnegative")
    return value


class LockWait:
    """Bound consecutive failed probes inside a caller's longer resource wait."""

    def __init__(self, path: Path, journal_root: Path | None, clock):
        self.path, self.journal_root, self.clock = path, journal_root, clock
        self.since = None

    @property
    def seconds_left(self) -> float:
        return math.inf if self.since is None else max(0.0, wait_limit() - (self.clock() - self.since))

    def remaining(self, acquired: bool) -> float:
        if error := _ABORTED.get():
            raise error
        if acquired:
            self.since = None
            return math.inf
        now = self.clock()
        if self.since is None:
            self.since = now
        limit = wait_limit()
        remaining = limit - (now - self.since)
        if remaining <= 0:
            _give_up(self.path, limit, self.journal_root)
        return remaining


def owner() -> dict:
    """Record self-reported ownership; inspecting processes belongs to timeout diagnostics."""
    return {"pid": os.getpid(), "command": list(sys.argv),
            "acquired_at": datetime.now(UTC).isoformat()}


def owner_path(path: Path) -> Path:
    """Where an exclusive holder is recorded: a side file, never the locked file itself.

    Some callers lock a data file directly (a lane's pending lesson notebook); writing the
    holder into it, or truncating it on release, erased its content (11-06).
    """
    return path.with_name(f".{path.name}.owner")


def adopt(descriptor: int, path: Path | None = None) -> None:
    """Record the process now owning an exclusive (possibly inherited) hold.

    With ``path`` the record goes to the side file; without it (a dedicated lock file held
    by descriptor only, such as the merge owner's) it is written into that lock file.
    """
    data = json.dumps(owner()).encode() + b"\n"
    if path is not None:
        owner_path(path).write_bytes(data)
        return
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, data)


def holders(path: Path) -> list[dict]:
    result = []
    for source in [owner_path(path), path, *path.parent.glob(f".{path.name}.reader-*")]:
        try:
            value = json.loads(source.read_bytes())
            if isinstance(value, dict) and type(value.get("pid")) is int:
                started, acquired = value.get("start_time"), value.get("acquired_at")
                if started is not None or isinstance(acquired, str):
                    try:
                        current = psutil.Process(value["pid"]).create_time()
                        if started is not None and current != started:
                            continue
                        # New metadata needs no acquisition-time probe: a process born
                        # after the recorded hold belongs to a reused PID.
                        if started is None and current > datetime.fromisoformat(acquired).timestamp():
                            continue
                    except psutil.NoSuchProcess:
                        continue
                    except (OSError, psutil.AccessDenied):
                        pass
                result.append(value)
        except (OSError, ValueError):
            continue
    if result:
        return result
    # A process started before ownership metadata was introduced can still hold
    # a kernel lock. lsof's lock field distinguishes holders from waiting openers.
    try:
        read = subprocess.run(["lsof", "-nP", "-Fpcfl", "--", str(path)],
                              capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return []
    process = {}
    for line in read.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            process = {"pid": int(line[1:])}
        elif line.startswith("c"):
            process["command"] = line[1:]
        elif line.startswith("l") and line[1:].strip() and process.get("pid"):
            if process not in result:
                result.append(dict(process))
    return result


def record_timeout(path: Path, seconds: float, journal_root: Path | None = None) -> None:
    """One O_APPEND write, without waiting again for the contended state lock."""
    from foragent.file_cache import invalidate

    root = journal_root or _JOURNAL.get() or path.parent
    root.mkdir(parents=True, exist_ok=True)
    event = {"at": datetime.now(UTC).isoformat(), "issue": None, "kind": "lock-timeout",
             "summary": f"pid {os.getpid()} gave up waiting for {path} after {seconds:g}s",
             "lock": str(path), "wait_seconds": seconds, "waiter": owner(), "holders": holders(path)}
    target = root / "orch-events.jsonl"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, json.dumps(event, ensure_ascii=True).encode() + b"\n")
    finally:
        os.close(descriptor)
    invalidate(target)


def _cancellable_sleep(seconds: float) -> None:
    """time.sleep, ended early when the caller's work is cancelled (a stopped `fa review`)."""
    from foragent import processes

    processes.pause(seconds, time.sleep)


def acquire(descriptor: int, path: Path, *, mode: int = fcntl.LOCK_EX,
            timeout: float | None = None, journal_root: Path | None = None,
            clock=None, sleep=None) -> None:
    """Poll with a monotonic deadline; even callers' larger limits are capped."""
    if error := _ABORTED.get():
        raise error
    clock, sleep = clock or time.monotonic, sleep or _cancellable_sleep
    limit = wait_limit()
    seconds = limit if timeout is None else min(timeout, limit)
    deadline = clock() + seconds
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - clock()
            if remaining <= 0:
                if timeout != 0:  # An explicit immediate probe defers without a timeout event.
                    _give_up(path, seconds, journal_root)
                raise LockTimeout(f"Timed out waiting for {path} after {seconds:g}s") from None
            sleep(min(POLL_SECONDS, remaining))
            continue
        break
    if mode == fcntl.LOCK_EX:
        try:
            adopt(descriptor, path)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise


def lock_order(path: Path) -> tuple[int, str] | None:
    """The order the guard checks: callers taking several locks sort by this key (None: unranked)."""
    rank = {"route.lock": -30, "merge.lock": -20, "foragent-publish.lock": -10,
            "capacity.lock": 10, "state.lock": 20, "memory-sample.lock": 30}.get(path.name)
    return (rank, str(path.resolve())) if rank is not None else None


@contextmanager
def locked(descriptor: int, path: Path, *, mode: int = fcntl.LOCK_EX,
           timeout: float | None = None, journal_root: Path | None = None,
           clock=None, sleep=None) -> Iterator[None]:
    order, held = lock_order(path), _HELD.get()
    if order is not None and held and order <= max(held):
        raise RuntimeError(f"Lock order inversion: {path} after {held}")
    journal = _JOURNAL.set(journal_root or _JOURNAL.get())
    token = None
    marker = None
    acquired = False
    try:
        acquire(descriptor, path, mode=mode, timeout=timeout, journal_root=journal_root, clock=clock, sleep=sleep)
        acquired = True
        token = _HELD.set((*held, order) if order is not None else held)
        if mode == fcntl.LOCK_SH:
            marker = path.with_name(f".{path.name}.reader-{os.getpid()}-{uuid4().hex}")
            marker.write_text(json.dumps(owner()), encoding="utf-8")
        yield
    finally:
        try:
            if marker is not None:
                marker.unlink(missing_ok=True)
            if acquired and mode == fcntl.LOCK_EX:
                owner_path(path).unlink(missing_ok=True)
        finally:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                if token is not None:
                    _HELD.reset(token)
                _JOURNAL.reset(journal)


@contextmanager
def file_lock(path: Path, *, timeout: float | None = None,
              journal_root: Path | None = None) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        with locked(stream.fileno(), path, timeout=timeout, journal_root=journal_root):
            yield

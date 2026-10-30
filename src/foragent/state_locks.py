"""File locks for the local state store and the acceptance baseline cache."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
import threading
import time
from typing import IO, Any

# Tick-lock paths this thread holds through StateStore.hold_tick_lock.
_TICK_LOCKS_HELD = threading.local()


def _held_tick_locks() -> set[str]:
    return _state._TICK_LOCKS_HELD.__dict__.setdefault("paths", set())


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX)
        try:
            yield
        finally:
            _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)


class LockBusy(Exception):
    """A timed lock stayed held by someone else for the whole wait."""


@contextmanager
def _timed_lock(path: Path, timeout: float) -> Iterator[None]:
    """Like ``_file_lock``, but polling a non-blocking lock every 0.1 s for up to ``timeout``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX | _state.fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)


class StateLocksMixin:
    def load_acceptance_baseline(self, key: str) -> dict[str, Any] | None:
        """Read a complete, unexpired baseline; old passing-only entries remain usable."""
        try:
            saved = _state.json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("timed_out") is True:
                return None  # A timed-out capture's history (#433) is incomplete, not a baseline (#435).
            age = self._now() - _state.datetime.fromisoformat(saved["created_at"])
            if not _state.timedelta() <= age < _state.timedelta(hours=6):
                return None
            for field in ("node_ids", "collected_ids", "failing_ids", "excluded"):
                if field != "node_ids" and field not in saved:
                    continue
                if not isinstance(saved[field], list) or not all(isinstance(node, str) for node in saved[field]):
                    return None
            if "collected_ids" in saved and "failing_ids" in saved:
                collected, passing, failing = (set(saved[field]) for field in
                                               ("collected_ids", "node_ids", "failing_ids"))
                if not passing <= collected or not failing <= collected or passing & failing:
                    return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return saved

    def lock_acceptance_baseline(self, key: str, *, blocking: bool = True) -> IO[str] | None:
        """Reserve a cache key until the last copy of the returned descriptor closes.

        A detached builder can inherit the descriptor without a gap in ownership.
        Closing the parent's copy must not explicitly unlock the child's copy.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock = (self.root / f"{key}.lock").open("a", encoding="utf-8")
        try:
            _state.fcntl.flock(lock.fileno(), _state.fcntl.LOCK_EX | (0 if blocking else _state.fcntl.LOCK_NB))
        except BlockingIOError:
            lock.close()
            return None
        except BaseException:
            lock.close()
            raise
        return lock

    def _state_lock(self) -> AbstractContextManager[None]:
        return _file_lock(self.root / "state.lock")

    def worktree_lock(self, number: int, timeout: float | None = None) -> AbstractContextManager[None]:
        """Serialize an issue's WIP preservation, worktree removal, talk decision and timed stop.

        Lock order: tick (if held) -> worktree -> state. Never wait for a
        worktree lock under the state lock, or take the tick lock under a
        worktree lock. These locks are not reentrant; cleanup acquires it
        separately from preservation. Keep lock files after removal so all
        contenders continue to lock the same inode. ``timeout`` None blocks;
        0 tries once; a number waits that long; a busy lock raises ``LockBusy``.
        """
        path = self.root / "runs" / f"issue-{number}.worktree.lock"
        return _file_lock(path) if timeout is None else _timed_lock(path, timeout)

    @contextmanager
    def try_worktree_lock(self, number: int) -> Iterator[bool]:
        """Take an issue's worktree lock without waiting; yield whether it was taken."""
        path = self.root / "runs" / f"issue-{number}.worktree.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._try_tick_lock(path) as acquired:
            yield acquired

    @contextmanager
    def merge_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "merge.lock").open("a", encoding="utf-8") as stream:
            _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX)
            try:
                yield
            finally:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)

    @contextmanager
    def notify_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "notify.lock").open("a", encoding="utf-8") as stream:
            _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX)
            try:
                yield
            finally:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)

    @contextmanager
    def tick_lock(self) -> Iterator[bool]:
        """Try the tick lock without waiting; yield whether this tick may run.

        Inside ``hold_tick_lock()`` on the same thread the lock is already held,
        so it yields True without taking it again.
        """
        path = self._tick_lock_path()
        if str(path.resolve()) in _state._held_tick_locks():
            yield True
            return
        with self._try_tick_lock(path) as acquired:
            yield acquired

    @contextmanager
    def hold_tick_lock(self) -> Iterator[bool]:
        """Take the tick lock so nested ``tick_lock()`` calls on this thread reenter it.

        The gate command holds it around a tick and the record of its outcome,
        so overlapping ticks record outcomes in the order they ran.
        """
        path = self._tick_lock_path()
        key, held = str(path.resolve()), _state._held_tick_locks()
        if key in held:
            yield True
            return
        with self._try_tick_lock(path) as acquired:
            if not acquired:
                yield False
                return
            held.add(key)
            try:
                yield True
            finally:
                held.discard(key)

    def _tick_lock_path(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / "tick.lock"

    @staticmethod
    @contextmanager
    def _try_tick_lock(path: Path) -> Iterator[bool]:
        with path.open("a", encoding="utf-8") as stream:
            try:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX | _state.fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

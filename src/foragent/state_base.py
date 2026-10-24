"""Shared storage primitives and locks for the local state store."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class StateBase:
    def __init__(self, root: Path, now: Callable[[], datetime] | None = None):
        self.root = _state.Path(root)
        self._now = now or (lambda: _state.datetime.now(_state.UTC))

    def _timestamp(self) -> str:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return now.astimezone(_state.UTC).isoformat()

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: _state.Path | None = None
        try:
            with _state.tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = _state.Path(stream.name)
                stream.write(content)
                stream.flush()
                _state.os.fsync(stream.fileno())
            _state.os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_write(path, _state.json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def worktree_path(self, number: int) -> Path:
        return self.root / "worktrees" / f"issue-{number}"

    def log_path(self, number: int) -> Path:
        return self.root / "logs" / f"issue-{number}.log"

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "state.lock").open("a", encoding="utf-8") as stream:
            _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX)
            try:
                yield
            finally:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)

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
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "tick.lock").open("a", encoding="utf-8") as stream:
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

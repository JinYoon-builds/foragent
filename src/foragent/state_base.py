"""Shared storage primitives for the local state store."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.state_records import Run


class StateBase:
    def __init__(self, root: Path, now: Callable[[], datetime] | None = None):
        self.root = _state.Path(root)
        self._now = now or (lambda: _state.datetime.now(_state.UTC))
        self._merge_lock_seconds: dict[tuple[int, str], float] = {}

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

    def _write_mapping(self, path: Path, cls: type, value: dict[str, Any]) -> dict[str, Any]:
        """Write a record mapping, keeping fields a newer version stored in the file."""
        try:
            saved = _state.json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        value = _state.write_back(cls, saved, value)
        self._write_json(path, value)
        return value

    def _write_record(self, path: Path, record: Any) -> dict[str, Any]:
        return self._write_mapping(path, type(record), _state.asdict(record))

    def _run_value(self, run: "Run") -> dict[str, Any]:
        """A run's fields merged over the unknown fields its file holds on disk."""
        try:
            saved = _state.json.loads((self.root / "runs" / f"{run.number}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        return _state.write_back(type(run), saved, _state.asdict(run))

    def _write_run(self, run: "Run", value: dict[str, Any] | None = None) -> dict[str, Any]:
        """The one writer of run files; unknown fields always survive."""
        value = self._run_value(run) if value is None else value
        self._write_json(self.root / "runs" / f"{run.number}.json", value)
        return value

    def worktree_path(self, number: int) -> Path:
        return self.root / "worktrees" / f"issue-{number}"

    def log_path(self, number: int) -> Path:
        return self.root / "logs" / f"issue-{number}.log"


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

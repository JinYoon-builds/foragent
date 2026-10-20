"""Atomic local snapshots and run records, independent of GitHub."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any


@dataclass(frozen=True)
class Approval:
    number: int
    body: str
    body_sha: str
    acceptance_blobs: dict[str, str]
    approved_by: str
    approved_at: str


@dataclass(frozen=True)
class Flag:
    number: int
    reason: str
    flagged_at: str


@dataclass(frozen=True)
class InspectState:
    since: str | None = None
    notified_signature: str | None = None


@dataclass(frozen=True)
class Run:
    number: int
    branch: str
    worktree: str
    pid: int | None
    started_at: str
    last_progress_at: str
    attempts: int = 1
    restarts: int = 0
    status: str = "running"
    outcome: str | None = None
    detail: str = ""


class StateStore:
    def __init__(self, root: Path, now: Callable[[], datetime] | None = None):
        self.root = Path(root)
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def for_repo(cls, repo: str, *, now: Callable[[], datetime] | None = None) -> "StateStore":
        return cls(Path.home() / ".local/share/foragent" / repo.replace("/", "__"), now)

    def _timestamp(self) -> str:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return now.astimezone(UTC).isoformat()

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def save_approval(
        self, number: int, *, body: str, body_sha: str,
        acceptance_blobs: dict[str, str], approved_by: str, approved_at: str,
    ) -> None:
        snapshot = {
            "number": number, "body": body, "body_sha": body_sha, "acceptance_blobs": acceptance_blobs,
            "approved_by": approved_by, "approved_at": approved_at,
        }
        self._write_json(self.root / "approvals" / f"{number}.json", snapshot)

    def load_approval(self, number: int) -> Approval | None:
        path = self.root / "approvals" / f"{number}.json"
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return Approval(**snapshot)

    def clear_approval(self, number: int) -> None:
        (self.root / "approvals" / f"{number}.json").unlink(missing_ok=True)

    def save_flag(self, number: int, reason: str, *, flagged_at: str | None = None) -> None:
        self._write_json(self.root / "flags" / f"{number}.json", {
            "number": number, "reason": reason,
            "flagged_at": flagged_at if flagged_at is not None else self._timestamp(),
        })

    def load_flag(self, number: int) -> Flag | None:
        try:
            value = (self.root / "flags" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return Flag(**json.loads(value))

    def load_inspect(self) -> InspectState:
        try:
            value = (self.root / "inspect.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return InspectState()
        return InspectState(**json.loads(value))

    def update_inspect(self, **fields: Any) -> InspectState:
        with self._state_lock():
            updated = replace(self.load_inspect(), **fields)
            self._write_json(self.root / "inspect.json", asdict(updated))
            return updated

    def start_run(
        self, number: int, *, branch: str, worktree: str | Path, pid: int | None = None,
    ) -> Run:
        with self._state_lock():
            timestamp = self._timestamp()
            run = Run(number, branch, str(worktree), pid, timestamp, timestamp)
            self._write_json(self.root / "runs" / f"{number}.json", asdict(run))
            return run

    def update_run(self, number: int, **fields: Any) -> Run:
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            updated = replace(run, **fields)
            self._write_json(self.root / "runs" / f"{number}.json", asdict(updated))
            return updated

    def get_run(self, number: int) -> Run | None:
        try:
            value = (self.root / "runs" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return Run(**json.loads(value))

    def active_runs(self) -> list[Run]:
        runs = []
        for path in sorted((self.root / "runs").glob("*.json"), key=lambda path: int(path.stem)):
            run = self.get_run(int(path.stem))
            if run is not None and run.status == "running":
                runs.append(run)
        return runs

    def finish_run(self, number: int, outcome: str, detail: str = "") -> Run:
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            finished = replace(run, status="finished", outcome=outcome, detail=detail)
            history_path = self.root / "runs.jsonl"
            history = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
            self._atomic_write(history_path, history + json.dumps(asdict(finished), ensure_ascii=False) + "\n")
            self._write_json(self.root / "runs" / f"{number}.json", asdict(finished))
            return finished

    def read_history(self) -> list[Run]:
        """Return each execution once, retaining its latest completion entry."""
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        runs: dict[tuple[int, str], Run] = {}
        for line in history.splitlines():
            run = Run(**json.loads(line))
            runs[run.number, run.started_at] = run
        return list(runs.values())

    def worktree_path(self, number: int) -> Path:
        return self.root / "worktrees" / f"issue-{number}"

    def log_path(self, number: int) -> Path:
        return self.root / "logs" / f"issue-{number}.log"

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "state.lock").open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def notify_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "notify.lock").open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def tick_lock(self) -> Iterator[bool]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "tick.lock").open("a", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

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
            saved = (self._read_issue_run(run.number) if run.kind == "issue" else
                     _state.json.loads(self._run_path(run).read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            saved = {}
        return _state.write_back(type(run), saved, _state.asdict(run))

    def _write_run(self, run: "Run", value: dict[str, Any] | None = None) -> dict[str, Any]:
        """History commits completion; runs/ keeps a compatibility snapshot, never the authority."""
        value = self._run_value(run) if value is None else value
        if run.kind == "issue" and run.status == "finished":
            path = self.root / "runs.jsonl"
            history = path.read_text(encoding="utf-8") if path.exists() else ""
            self._atomic_write(path, history + _state.json.dumps(value, ensure_ascii=False) + "\n")
        self._write_json(self._run_path(run), value)
        return value

    def _read_issue_run(self, number: int) -> dict[str, Any]:
        """Resolve the current execution's completion from the same facts history reads.

        The snapshot identifies the execution; its cached completion can lag after a crash.
        Old snapshots remain readable until the next writer adopts the journal protocol.
        """
        value = _state.json.loads((self.root / "runs" / f"{number}.json").read_text(encoding="utf-8"))
        if not value.get("run_generation"):
            return value
        for row in reversed(self._run_history()):
            if (row["number"] == number and row["started_at"] == value["started_at"]
                    and row.get("run_generation") == value.get("run_generation")):
                return dict(row)
        return value

    def _run_history(self) -> list[dict[str, Any]]:
        """Completed facts, cached only while the atomically replaced file is unchanged."""
        path = self.root / "runs.jsonl"
        try:
            with path.open(encoding="utf-8") as stream:
                stat = _state.os.fstat(stream.fileno())
                key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                cached = getattr(self, "_run_history_cache", None)
                if cached is None or cached[0] != key:
                    cached = (key, [_state.json.loads(line) for line in stream])
                    self._run_history_cache = cached
        except FileNotFoundError:
            return []
        return cached[1]

    def _run_path(self, run: "Run") -> Path:
        """runs/<n>.json for an issue run; dream-<lane>.json or draft-<n>.json for a side run (Run.key)."""
        return self.root / "runs" / f"{run.key}.json"

    def get_dream(self, lane: str) -> "Run | None":
        """A lane's dream run record; it never enters runs.jsonl (that history is issue-only)."""
        try:
            value = (self.root / "runs" / f"dream-{lane}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Run, _state.json.loads(value))

    def dream_runs(self) -> list["Run"]:
        """Every lane's dream record, running or finished (runs/dream-<lane>.json)."""
        paths = sorted((self.root / "runs").glob("dream-*.json"))
        return [run for path in paths if (run := self.get_dream(path.stem[6:])) is not None]

    def start_dream(self, lane: str, *, worktree: str | Path, under_machine_lock: bool = False) -> "Run":
        """Write a fresh dream record.

        ``under_machine_lock``: as prepare_run_start's reservation, the tick writes it while
        holding the machine lock alone (one lock at a time, the order `fa orch facts` relies on).
        A new dream record has no other writer then: the tick lock admits one tick, and an
        earlier worker of the lane finished its record as its last act.
        """
        if not under_machine_lock:
            with self._state_lock():
                return self.start_dream(lane, worktree=worktree, under_machine_lock=True)
        # A fresh record, as start_run's for an issue: no earlier birth identity may judge this
        # dream's worker. The lane does not dream while an earlier dream still owns processes.
        timestamp = self._timestamp()
        run = _state.Run(0, f"foragent/dream-{lane}", str(worktree), None, timestamp, timestamp,
                         lane=lane, tier="s", last_activity_at=timestamp, kind="dream")
        self._write_json(self.root / "runs" / f"dream-{lane}.json", _state.asdict(run))
        return run

    def get_draft(self, number: int) -> "Run | None":
        """An issue's test-drafting run record (runs/draft-<n>.json); like a dream's, never in runs.jsonl."""
        try:
            value = (self.root / "runs" / f"draft-{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Run, _state.json.loads(value))

    def draft_runs(self) -> list["Run"]:
        """Every issue's drafting record, running or finished."""
        paths = sorted((self.root / "runs").glob("draft-*.json"))
        return [run for path in paths if path.stem[6:].isdigit() and (run := self.get_draft(int(path.stem[6:])))]

    def start_draft(self, number: int, lane: str, *, worktree: str | Path, attempts: int = 1,
                    under_machine_lock: bool = False) -> "Run":
        """Write a fresh drafting record, as start_dream writes a dream's (the tick holds the machine lock).

        ``attempts`` counts the sessions that used an attempt, this one included; the record is the
        issue's only durable drafting fact (journals are trimmed), so the tick carries it forward.
        """
        if not under_machine_lock:
            with self._state_lock():
                return self.start_draft(number, lane, worktree=worktree, attempts=attempts, under_machine_lock=True)
        timestamp = self._timestamp()
        run = _state.Run(number, f"foragent/draft-{number}", str(worktree), None, timestamp, timestamp,
                         lane=lane, tier="s", last_activity_at=timestamp, kind="draft", attempts=attempts)
        self._write_json(self.root / "runs" / f"draft-{number}.json", _state.asdict(run))
        return run

    def finish_dream(self, lane: str, outcome: str, detail: str = "") -> "Run | None":
        return self._finish_side(f"dream-{lane}", outcome, detail)

    def _finish_side(self, key: str, outcome: str, detail: str = "") -> "Run | None":
        """Finish a dream or draft record; side runs never enter runs.jsonl."""
        with self._state_lock():
            run = self.get_dream(key[6:]) if key.startswith("dream-") else self.get_draft(int(key[6:]))
            if run is None or run.status == "finished":
                return run
            from foragent.processes import observe_tree

            # As finish_run: observe the recorded tree once more, so its descendants stay owned.
            finished = _state.replace(run, status="finished", outcome=outcome,
                                      detail=detail.strip() or run.detail or f"Run finished with outcome {outcome}.",
                                      process_identities=observe_tree(run.process_identities))
            self._write_run(finished, {**self._run_value(finished), "finished_at": self._timestamp()})
            return finished

    def worktree_path(self, number: int) -> Path:
        return self.root / "worktrees" / f"issue-{number}"

    def log_path(self, number: int) -> Path:
        return self.root / "logs" / f"issue-{number}.log"


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

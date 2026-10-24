"""Approvals, flags, inspect state, run records, pauses and run history."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.state_records import Approval, Flag, InspectState, Run


class RunRecordsMixin:
    def order_progress(self) -> dict[str, Any] | None:
        """Read order preparation facts shared by the CLI and lane view."""
        try:
            return _state.json.loads((self.root / "order-progress.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def update_order_progress(
        self, update: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Apply one local fact under a lock; None ends preparation."""
        with self._state_lock():
            updated = update(self.order_progress())
            path = self.root / "order-progress.json"
            if updated is None:
                path.unlink(missing_ok=True)
            else:
                self._write_json(path, updated)
            return updated

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
            snapshot = _state.json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return _state.Approval(**snapshot)

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
        return _state.Flag(**_state.json.loads(value))

    def load_inspect(self) -> InspectState:
        try:
            value = (self.root / "inspect.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return _state.InspectState()
        return _state.InspectState(**_state.json.loads(value))

    def update_inspect(self, **fields: Any) -> InspectState:
        with self._state_lock():
            updated = _state.replace(self.load_inspect(), **fields)
            self._write_json(self.root / "inspect.json", _state.asdict(updated))
            return updated

    def start_run(
        self, number: int, *, branch: str, worktree: str | Path, pid: int | None = None,
        lane: str = "main", tier: str = "m",
    ) -> Run:
        with self._state_lock():
            timestamp = self._timestamp()
            approval = self.load_approval(number)
            previous = self.get_run(number)
            restarts = previous.restarts if previous is not None and previous.outcome in {"stalled-restart", "crashed", "limit-wait"} else 0
            attempts = previous.attempts if previous is not None and previous.outcome == "limit-wait" else 1
            resumed = previous is not None and previous.outcome == "limit-wait"
            session = {}
            if previous is not None:
                reason = "\n".join(value for value in (
                    previous.outcome, previous.detail, previous.resume_reason,
                ) if value)
                session = {
                    "session_id": previous.session_id, "session_executor": previous.session_executor,
                    "session_ids": previous.session_ids, "session_body": previous.session_body,
                    "merge_conflicts": previous.merge_conflicts,
                    "local_skill_paths": previous.local_skill_paths,
                    "resume_reason": reason + (
                        "\nThe runner recreated the worktree from its target branch; "
                        "earlier uncommitted changes are no longer present."
                    ),
                }
            run = _state.Run(number, branch, str(worktree), pid, timestamp, timestamp, attempts=attempts,
                      restarts=restarts, lane=lane, tier=previous.tier if resumed else tier,
                      attempt_tiers=previous.attempt_tiers if resumed else {},
                      approval_body_sha=approval.body_sha if approval is not None else None,
                      last_activity_at=timestamp, **session)
            self._write_json(self.root / "runs" / f"{number}.json", _state.asdict(run))
            return run

    def prepare_run_start(self, number: int) -> None:
        """Journal the label transition before a runner or run record exists."""
        self._write_json(self.root / "run-starts" / f"{number}.json", {
            "number": number, "prepared_at": self._timestamp(),
        })

    def run_start_pending(self, number: int) -> bool:
        return (self.root / "run-starts" / f"{number}.json").exists()

    def run_starts(self) -> dict[int, str]:
        """Map each journaled, not yet cleared start to its prepared_at time."""
        starts = {}
        for path in (self.root / "run-starts").glob("*.json"):
            value = _state.json.loads(path.read_text(encoding="utf-8"))
            starts[int(value["number"])] = value["prepared_at"]
        return starts

    def clear_run_start(self, number: int) -> None:
        (self.root / "run-starts" / f"{number}.json").unlink(missing_ok=True)

    def update_run(self, number: int, **fields: Any) -> Run:
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            if "detail" in fields:
                fields["detail"] = (
                    " ".join(fields["detail"].split())[:500]
                    or " ".join(run.detail.split())[:500]
                    or "Run detail unavailable."
                )
            updated = _state.replace(run, **fields)
            self._write_json(self.root / "runs" / f"{number}.json", _state.asdict(updated))
            return updated

    def get_run(self, number: int) -> Run | None:
        try:
            value = (self.root / "runs" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _state.Run(**_state.json.loads(value))

    def active_runs(self) -> list[Run]:
        runs = []
        for path in sorted((self.root / "runs").glob("*.json"), key=lambda path: int(path.stem)):
            run = self.get_run(int(path.stem))
            if run is not None and run.status == "running":
                runs.append(run)
        return runs

    def pause_lane(self, lane: str, until: datetime) -> None:
        with self._state_lock():
            path = self.root / "lane-pauses.json"
            pauses = _state.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            timestamp = until.astimezone(_state.UTC).isoformat()
            pauses[lane] = max(pauses.get(lane, timestamp), timestamp)
            self._write_json(path, pauses)

    def lane_paused(self, lane: str, now: datetime) -> bool:
        try:
            pauses = _state.json.loads((self.root / "lane-pauses.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        return lane in pauses and now < _state.datetime.fromisoformat(pauses[lane])

    def pause_github(self, until: datetime) -> datetime:
        """Share a reset deadline across runners and ticks without shortening it."""
        with self._state_lock():
            until = (self.github_wait_until(until) or until).astimezone(_state.UTC)
            self._write_json(self.root / "github-pause.json", {"until": until.isoformat()})
            return until

    def github_wait_until(self, now: datetime) -> datetime | None:
        try:
            value = (self.root / "github-pause.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        until = _state.datetime.fromisoformat(_state.json.loads(value)["until"])
        return until if now < until else None

    def finish_run(self, number: int, outcome: str, detail: str = "") -> Run:
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            detail = " ".join(detail.split())[:500]
            if not detail and outcome != "merged":
                detail = " ".join(run.detail.split())[:500] or f"Run finished with outcome {outcome}."
            finished = _state.replace(run, status="finished", outcome=outcome, detail=detail)
            history_path = self.root / "runs.jsonl"
            history = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
            self._atomic_write(history_path, history + _state.json.dumps(_state.asdict(finished), ensure_ascii=False) + "\n")
            self._write_json(self.root / "runs" / f"{number}.json", _state.asdict(finished))
            return finished

    def read_history(self) -> list[Run]:
        """Return each execution once, retaining its latest completion entry."""
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        runs: dict[tuple[int, str], _state.Run] = {}
        for line in history.splitlines():
            run = _state.Run(**_state.json.loads(line))
            runs[run.number, run.started_at] = run
        return list(runs.values())


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

"""Atomic local snapshots and run records, independent of GitHub."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
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
    notified_items: list[str] = field(default_factory=list)
    summary_date: str | None = None


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
    lane: str = "main"
    waited_seconds: float = 0
    waiting_until: str | None = None
    transient_errors: int = 0
    fallback_model: str | None = None
    executor_waited_seconds: float = 0
    tier: str = "m"
    attempt_tiers: dict[str, str] = field(default_factory=dict)
    phase: str | None = None
    phase_started_at: str | None = None
    last_activity_at: str | None = None
    executor_pgid: int | None = None
    session_id: str | None = None
    session_executor: str | None = None
    session_ids: list[str] = field(default_factory=list)
    session_body: str | None = None
    resume_reason: str = ""


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

    def _issue_snapshot(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "issues.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"refreshed_at": None, "issues": {}}

    def local_issues(self) -> list[dict[str, Any]]:
        """Read the last observed issue facts without contacting the tracker."""
        return sorted(self._issue_snapshot()["issues"].values(), key=lambda row: row["number"])

    def issues_revision(self) -> int:
        return self._issue_snapshot().get("revision", 0)

    def _save_local_issue(self, snapshot: dict[str, Any], row: dict[str, Any]) -> None:
        snapshot["revision"] = snapshot.get("revision", 0) + 1
        row.update(revision=snapshot["revision"], updated_at=self._timestamp())
        self._write_json(self.root / "issues.json", snapshot)

    def issues_refresh_due(self, now: datetime) -> bool:
        refreshed = self._issue_snapshot()["refreshed_at"]
        return refreshed is None or now - datetime.fromisoformat(refreshed) >= timedelta(minutes=5)

    def update_local_issue(self, number: int, **fields: Any) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].setdefault(str(number), {
                "number": number, "title": "", "body": "", "labels": [], "lane": "main",
                "state": "open", "pr_number": None, "merged": False, "comments": [],
            })
            row.update(fields)
            self._save_local_issue(snapshot, row)

    def record_issue(self, issue: Any, lanes: dict) -> None:
        from foragent.labels import issue_lane

        if not issue.is_pull_request:
            self.update_local_issue(issue.number, title=issue.title, body=issue.body,
                                    labels=sorted(issue.labels), state=issue.state,
                                    lane=issue_lane(issue.labels, lanes))

    def record_labels(self, number: int, labels: list[str], *, remove: bool, lanes: dict) -> None:
        from foragent.labels import READY, issue_lane

        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].setdefault(str(number), {
                "number": number, "title": "", "body": "", "labels": [], "lane": "main",
                "state": "open", "pr_number": None, "merged": False, "comments": [],
            })
            current = set(row["labels"])
            row["labels"] = sorted(current - set(labels) if remove else current | set(labels))
            row["lane"] = issue_lane(row["labels"], lanes)
            if READY in labels:
                approval = self.load_approval(number)
                row["ready_approval"] = None if remove or approval is None else approval.approved_at
            self._save_local_issue(snapshot, row)

    def record_comments(self, number: int, comments: list[dict], *, append: bool = False) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is not None:
                row["comments"] = row["comments"] + comments if append else comments
                self._save_local_issue(snapshot, row)

    def record_board_value(self, number: int, project: str, field: str, value: str) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is not None:
                row.setdefault("board_values", {}).setdefault(project, {})[field] = value
                self._save_local_issue(snapshot, row)

    def refresh_local_issues(
        self, issues: list, prs: list, lanes: dict, now: datetime,
        *, comments: dict[int, list[dict]] | None = None, since_revision: int | None = None,
    ) -> None:
        """Replace remote facts, preserving local changes made since the refresh began."""
        from foragent.labels import issue_lane
        from foragent.tracker import pr_issue_number

        comments = comments or {}
        with self._state_lock():
            snapshot = self._issue_snapshot()
            previous = snapshot["issues"]
            rows = {}
            for issue in issues:
                if issue.is_pull_request:
                    continue
                old = previous.get(str(issue.number), {})
                rows[str(issue.number)] = {
                    **old, "number": issue.number, "title": issue.title, "body": issue.body,
                    "labels": sorted(issue.labels), "lane": issue_lane(issue.labels, lanes),
                    "state": issue.state, "pr_number": old.get("pr_number"),
                    "merged": old.get("merged", False),
                    "comments": comments.get(issue.number, old.get("comments", [])),
                    "updated_at": self._timestamp(),
                }
            for pr in prs:
                if (row := rows.get(str(pr_issue_number(pr.head)))) is not None:
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
            if since_revision is not None:
                rows.update({number: row for number, row in previous.items()
                             if row.get("revision", 0) > since_revision})
            self._write_json(self.root / "issues.json", {
                **snapshot, "refreshed_at": now.isoformat(), "issues": rows,
            })

    def order_progress(self) -> dict[str, Any] | None:
        """Read order preparation facts shared by the CLI and lane view."""
        try:
            return json.loads((self.root / "order-progress.json").read_text(encoding="utf-8"))
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
        lane: str = "main", tier: str = "m",
    ) -> Run:
        with self._state_lock():
            timestamp = self._timestamp()
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
                    "resume_reason": reason + (
                        "\nThe runner recreated the worktree from its target branch; "
                        "earlier uncommitted changes are no longer present."
                    ),
                }
            run = Run(number, branch, str(worktree), pid, timestamp, timestamp, attempts=attempts,
                      restarts=restarts, lane=lane, tier=previous.tier if resumed else tier,
                      attempt_tiers=previous.attempt_tiers if resumed else {},
                      last_activity_at=timestamp, **session)
            self._write_json(self.root / "runs" / f"{number}.json", asdict(run))
            return run

    def prepare_run_start(self, number: int) -> None:
        """Journal the label transition before a runner or run record exists."""
        self._write_json(self.root / "run-starts" / f"{number}.json", {
            "number": number, "prepared_at": self._timestamp(),
        })

    def run_start_pending(self, number: int) -> bool:
        return (self.root / "run-starts" / f"{number}.json").exists()

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

    def pause_lane(self, lane: str, until: datetime) -> None:
        with self._state_lock():
            path = self.root / "lane-pauses.json"
            pauses = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            timestamp = until.astimezone(UTC).isoformat()
            pauses[lane] = max(pauses.get(lane, timestamp), timestamp)
            self._write_json(path, pauses)

    def lane_paused(self, lane: str, now: datetime) -> bool:
        try:
            pauses = json.loads((self.root / "lane-pauses.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        return lane in pauses and now < datetime.fromisoformat(pauses[lane])

    def pause_github(self, until: datetime) -> datetime:
        """Share a reset deadline across runners and ticks without shortening it."""
        with self._state_lock():
            until = (self.github_wait_until(until) or until).astimezone(UTC)
            self._write_json(self.root / "github-pause.json", {"until": until.isoformat()})
            return until

    def github_wait_until(self, now: datetime) -> datetime | None:
        try:
            value = (self.root / "github-pause.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        until = datetime.fromisoformat(json.loads(value)["until"])
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
    def merge_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "merge.lock").open("a", encoding="utf-8") as stream:
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

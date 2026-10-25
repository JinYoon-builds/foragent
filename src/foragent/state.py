"""Atomic local snapshots and run records, independent of GitHub."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import IO, Any

from foragent.records import load_record, write_back

# Tick-lock paths this thread holds through StateStore.hold_tick_lock.
_TICK_LOCKS_HELD = threading.local()


def _held_tick_locks() -> set[str]:
    return _TICK_LOCKS_HELD.__dict__.setdefault("paths", set())


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
    process_identities: list[dict] = field(default_factory=list)
    session_id: str | None = None
    session_executor: str | None = None
    session_ids: list[str] = field(default_factory=list)
    session_body: str | None = None
    resume_reason: str = ""
    resume_failures: int = 0
    merge_conflicts: list[str] = field(default_factory=list)
    local_skill_paths: list[str] = field(default_factory=list)
    approval_body_sha: str | None = None


REFRESH_INTERVAL = timedelta(minutes=5)


def observed_issue_facts(issue: Any) -> dict[str, Any]:
    """The facts one bulk listing shows for an issue; a change in any of them marks it changed."""
    def counts(summary: Any) -> dict[str, int] | None:
        return dict(summary) if summary is not None else None

    return {"title": issue.title, "body": issue.body, "labels": sorted(issue.labels),
            "state": issue.state, "updated_at": issue.updated_at,
            # Link counts change with a sub-issue or dependency change even if updated_at does not.
            "sub_issues": counts(getattr(issue, "sub_issues_summary", None)),
            "dependencies": counts(getattr(issue, "dependencies_summary", None))}


def _forget_reads(row: dict[str, Any]) -> None:
    """A local change to an issue makes its earlier per-issue reads unconfirmed."""
    row.pop("reads", None)


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

    def load_acceptance_baseline(self, key: str) -> dict[str, Any] | None:
        """Read a complete, unexpired baseline; old passing-only entries remain usable."""
        try:
            saved = json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))
            age = self._now() - datetime.fromisoformat(saved["created_at"])
            if not timedelta() <= age < timedelta(hours=6):
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
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            lock.close()
            return None
        except BaseException:
            lock.close()
            raise
        return lock

    def _issue_snapshot(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "issues.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"refreshed_at": None, "issues": {}}

    def local_issues(self) -> list[dict[str, Any]]:
        """Read the last observed issue facts without contacting the tracker."""
        return sorted(self._issue_snapshot()["issues"].values(), key=lambda row: row["number"])

    def github_outbox(self) -> list[dict[str, Any]]:
        """Read pending GitHub writes in their persisted delivery order."""
        return self._issue_snapshot().get("outbox", [])

    def drop_missing_github_issue(self, number: int) -> None:
        """Remove a confirmed missing issue and all its pending writes atomically."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            snapshot["outbox"] = [entry for entry in snapshot.get("outbox", [])
                                  if entry["issue"] != number]
            snapshot.get("outbox_base", {}).pop(str(number), None)
            snapshot["issues"].pop(str(number), None)
            snapshot["revision"] = snapshot.get("revision", 0) + 1
            self._write_json(self.root / "issues.json", snapshot)

    def _local_issue(self, snapshot: dict[str, Any], number: int) -> dict[str, Any]:
        return snapshot["issues"].setdefault(str(number), {
            "number": number, "title": "", "body": "", "labels": [], "lane": "main",
            "state": "open", "pr_number": None, "merged": False, "comments": [],
        })

    def _apply_github_write(self, row: dict[str, Any], entry: dict[str, Any], lanes: dict) -> None:
        """Apply the same write facts to local intent and its delivery baseline."""
        action = entry["action"]
        if action != "comment":
            _forget_reads(row)
        if action in {"add_labels", "remove_labels"}:
            self._record_labels(row, entry["labels"], remove=action == "remove_labels", lanes=lanes)
        elif action == "update_issue":
            row.update(title=entry["title"], body=entry["body"])
        elif action == "close_issue":
            row["state"] = "closed"
        if action == "comment" or action == "close_issue" and entry.get("body") is not None:
            row["comments"] = row.get("comments", []) + [{
                "id": -entry["id"], "body": entry["body"], "author": "foragent",
                "created_at": entry["queued_at"],
            }]

    def queue_github_write(self, number: int, action: str, *, lanes: dict | None = None,
                           **payload: Any) -> dict[str, Any]:
        """Persist the FIFO entry and its local effect in one transaction."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            snapshot.setdefault("outbox_base", {}).setdefault(str(number), deepcopy(row))
            identity = snapshot.get("outbox_sequence", 0) + 1
            entry = {"id": identity, "issue": number, "action": action,
                     "queued_at": self._timestamp(), **payload}
            snapshot.setdefault("outbox", []).append(entry)
            snapshot["outbox_sequence"] = identity
            self._apply_github_write(row, entry, lanes or {})
            self._save_local_issue(snapshot, row)
            return entry

    def deliver_github_write(self, entry: dict[str, Any], *, comment_id: int | None = None,
                             remaining_labels: list[str] | None = None) -> None:
        """Acknowledge delivery without losing concurrent appends or later local intent."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            pending = snapshot.get("outbox", [])
            item = next((item for item in pending if item["id"] == entry["id"]), None)
            if item is None:
                return
            delivered = dict(item)
            if remaining_labels is not None:
                delivered["labels"] = item["labels"][:len(item["labels"]) - len(remaining_labels)]
                item["labels"] = remaining_labels
            baseline = snapshot.get("outbox_base", {}).get(str(entry["issue"]))
            if baseline is not None:
                self._apply_github_write(baseline, delivered, {})
            if not remaining_labels:
                snapshot["outbox"] = [item for item in pending if item["id"] != entry["id"]]
            row = self._local_issue(snapshot, entry["issue"])
            if comment_id is not None:
                for facts in (row, baseline or {}):
                    for comment in facts.get("comments", []):
                        if comment["id"] == -entry["id"]:
                            comment["id"] = comment_id
            if not any(item["issue"] == entry["issue"] for item in snapshot.get("outbox", [])):
                snapshot.get("outbox_base", {}).pop(str(entry["issue"]), None)
            elif baseline is not None:
                baseline["revision"] = snapshot.get("revision", 0) + 1
            self._save_local_issue(snapshot, row)

    def issues_observation(self) -> dict[str, Any]:
        """When the snapshot was last read and whether a later read failed."""
        snapshot = self._issue_snapshot()
        failure = snapshot.get("refresh_failure") or {}
        return {"as_of": snapshot["refreshed_at"], "error": failure.get("error"),
                "failed_at": failure.get("failed_at")}

    def record_issues_refresh_failure(self, error: str, failed_at: datetime) -> None:
        """Keep the last known facts and note that a later read failed."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            snapshot["refresh_failure"] = {"error": error, "failed_at": failed_at.isoformat()}
            self._write_json(self.root / "issues.json", snapshot)

    def issues_revision(self) -> int:
        return self._issue_snapshot().get("revision", 0)

    def _save_local_issue(self, snapshot: dict[str, Any], row: dict[str, Any]) -> None:
        snapshot["revision"] = snapshot.get("revision", 0) + 1
        row.update(revision=snapshot["revision"], updated_at=self._timestamp())
        self._write_json(self.root / "issues.json", snapshot)

    def issues_refresh_due(self, now: datetime) -> bool:
        refreshed = self._issue_snapshot()["refreshed_at"]
        return refreshed is None or now - datetime.fromisoformat(refreshed) >= REFRESH_INTERVAL

    def update_local_issue(self, number: int, **fields: Any) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            if any(row.get(key) != value for key, value in fields.items()):
                _forget_reads(row)
            row.update(fields)
            self._save_local_issue(snapshot, row)

    def record_issue(self, issue: Any, lanes: dict) -> None:
        from foragent.labels import issue_lane

        with self._state_lock():
            snapshot = self._issue_snapshot()
            if issue.is_pull_request or any(
                entry["issue"] == issue.number for entry in snapshot.get("outbox", [])
            ):
                return
            row = self._local_issue(snapshot, issue.number)
            facts = dict(title=issue.title, body=issue.body, labels=sorted(issue.labels), state=issue.state)
            if any(row.get(key) != value for key, value in facts.items()):
                _forget_reads(row)
            row.update(facts, lane=issue_lane(issue.labels, lanes))
            self._save_local_issue(snapshot, row)

    def record_labels(self, number: int, labels: list[str], *, remove: bool, lanes: dict) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            self._record_labels(row, labels, remove=remove, lanes=lanes)
            self._save_local_issue(snapshot, row)

    def _record_labels(self, row: dict[str, Any], labels: list[str], *, remove: bool, lanes: dict) -> None:
        from foragent.labels import READY, issue_lane

        current = set(row["labels"])
        row["labels"] = sorted(current - set(labels) if remove else current | set(labels))
        if row["labels"] != sorted(current):
            _forget_reads(row)
        row["lane"] = issue_lane(row["labels"], lanes)
        if READY in labels:
            approval = self.load_approval(row["number"])
            row["ready_approval"] = None if remove or approval is None else approval.approved_at

    def record_comments(self, number: int, comments: list[dict], *, append: bool = False) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            if not append and any(entry["issue"] == number for entry in snapshot.get("outbox", [])):
                return
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
        parents: dict[int, int] | None = None, blocked_by: dict[int, list[dict]] | None = None,
        children: dict[int, list[int]] | None = None, complete_prs: bool = False,
    ) -> list[int]:
        """Replace remote facts, preserving local changes made since the refresh began."""
        from foragent.labels import issue_lane
        from foragent.tracker import pr_issue_number

        comments = comments or {}
        with self._state_lock():
            snapshot = self._issue_snapshot()
            previous = snapshot["issues"]
            rows = {}
            facts = {}
            observed = {issue.number for issue in issues if not issue.is_pull_request}
            dropped = {entry["issue"] for entry in snapshot.get("outbox", [])
                       if entry["issue"] not in observed and (
                           since_revision is None or snapshot.get("outbox_base", {}).get(
                               str(entry["issue"]), {}).get("revision", 0) <= since_revision)}
            for issue in issues:
                if issue.is_pull_request:
                    continue
                old = previous.get(str(issue.number), {})
                pending = [entry for entry in snapshot.get("outbox", []) if entry["issue"] == issue.number]
                old_comments = old.get("comments", [])
                queued_ids = {-entry["id"] for entry in pending}
                old_comments = [comment for comment in old_comments if comment["id"] not in queued_ids]
                facts[str(issue.number)] = observed_issue_facts(issue)
                remote = {
                    **old, "number": issue.number, "title": issue.title, "body": issue.body,
                    "labels": sorted(issue.labels), "lane": issue_lane(issue.labels, lanes),
                    "state": issue.state,
                    "pr_number": None if complete_prs else old.get("pr_number"),
                    "merged": False if complete_prs else old.get("merged", False),
                    "comments": comments.get(issue.number, old_comments),
                    "updated_at": self._timestamp(),
                }
                if pending:
                    # Pending effects are already in old. Compare GitHub with
                    # the same snapshot before those effects, advanced only by
                    # acknowledged writes, so a partial delivery cannot hide a
                    # person's change under the remaining local intent.
                    # Only labels and open/closed state conflict here; discussion
                    # content is independent, and comment snapshots may be incomplete.
                    expected = snapshot.get("outbox_base", {}).get(str(issue.number), {})
                    concurrent = since_revision is not None and expected.get("revision", 0) > since_revision
                    fields = ("labels", "state")
                    matches = all(remote[key] == expected.get(key) for key in fields)
                    if concurrent or matches:
                        rows[str(issue.number)] = dict(old)
                        continue
                    dropped.add(issue.number)
                rows[str(issue.number)] = remote
            for number, row in rows.items():
                # Order edges and dependencies are observations, never local intent.
                if parents is not None:
                    row["parent"] = parents.get(int(number))
                if blocked_by is not None:
                    row["blocked_by"] = blocked_by.get(int(number), [])
                if children is not None:
                    row["children"] = children.get(int(number), [])
            pr_facts: dict[str, dict[str, Any]] = {}
            for pr in prs:
                if (row := rows.get(str(pr_issue_number(pr.head)))) is not None:
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
                    pr_facts[str(pr_issue_number(pr.head))] = {
                        "pr_number": pr.number, "pr_head": pr.head, "merged": pr.merged_at is not None}
            for number, row in rows.items():
                # What this listing showed, kept apart from later local intent.
                listed = (pr_facts.get(number, {"pr_number": None, "pr_head": None, "merged": False})
                          if complete_prs else {key: row.get(key) for key in ("pr_number", "pr_head", "merged")})
                row["observed"] = {**facts[number], **listed}
            if since_revision is not None:
                rows.update({number: row for number, row in previous.items()
                             if row.get("revision", 0) > since_revision and int(number) not in dropped})
            snapshot.pop("refresh_failure", None)
            self._write_json(self.root / "issues.json", {
                **snapshot, "refreshed_at": now.isoformat(), "issues": rows,
                "outbox": [entry for entry in snapshot.get("outbox", []) if entry["issue"] not in dropped],
                "outbox_base": {number: row for number, row in snapshot.get("outbox_base", {}).items()
                                if int(number) not in dropped},
            })
            return sorted(dropped)

    def read_baseline(self) -> dict[str, Any]:
        """Facts a per-issue read will depend on, taken before the read starts."""
        snapshot = self._issue_snapshot()
        return {"refresh": snapshot["refreshed_at"],
                "observed": {key: row.get("observed") for key, row in snapshot["issues"].items()}}

    def confirmed_read(self, number: int, name: str) -> tuple[Any] | None:
        """Return a remembered per-issue read that a later bulk refresh confirmed.

        A read stays valid while the latest listing, taken after the read, shows
        the same facts (including GitHub's updated_at and link counts) for the
        issue and for each linked issue, such as its blockers. A link that is
        not a row of this repository's listing (another repository, or a missing
        row) can never confirm a read. Without a refresh after the read,
        nothing confirms it, so the caller reads again.
        """
        snapshot = self._issue_snapshot()
        rows = snapshot["issues"]
        entry = (rows.get(str(number)) or {}).get("reads", {}).get(name)
        refreshed = snapshot["refreshed_at"]
        if entry is None or refreshed is None or entry["refresh"] is None:
            return None
        if datetime.fromisoformat(refreshed) <= datetime.fromisoformat(entry["refresh"]):
            return None
        for key, facts in entry["observed"].items():
            if facts is None or (rows.get(key) or {}).get("observed") != facts:
                return None
        return (entry["value"],)

    def record_read(self, number: int, name: str, value: Any, *, baseline: dict[str, Any],
                    links: Iterable[int | str] = ()) -> None:
        """Remember a per-issue read with the facts captured before it began.

        A link is this repository's issue number; any other link (such as
        "owner/repo#5") names no row, so it keeps the read unconfirmed.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is None:
                return
            keys = [str(number), *(str(link) for link in links)]
            row.setdefault("reads", {})[name] = {
                "value": value, "refresh": baseline["refresh"],
                "observed": {key: baseline["observed"].get(key) for key in keys},
            }
            # A remembered read is not an issue change; keep revisions for real writes.
            self._write_json(self.root / "issues.json", snapshot)

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
        self._write_mapping(self.root / "approvals" / f"{number}.json", Approval, snapshot)

    def load_approval(self, number: int) -> Approval | None:
        path = self.root / "approvals" / f"{number}.json"
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return load_record(Approval, snapshot)

    def clear_approval(self, number: int) -> None:
        (self.root / "approvals" / f"{number}.json").unlink(missing_ok=True)

    def save_flag(self, number: int, reason: str, *, flagged_at: str | None = None) -> None:
        self._write_mapping(self.root / "flags" / f"{number}.json", Flag, {
            "number": number, "reason": reason,
            "flagged_at": flagged_at if flagged_at is not None else self._timestamp(),
        })

    def load_flag(self, number: int) -> Flag | None:
        try:
            value = (self.root / "flags" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return load_record(Flag, json.loads(value))

    def load_inspect(self) -> InspectState:
        try:
            value = (self.root / "inspect.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return InspectState()
        return load_record(InspectState, json.loads(value))

    def _write_mapping(self, path: Path, cls: type, value: dict[str, Any]) -> dict[str, Any]:
        """Write a record mapping, keeping fields a newer version stored in the file."""
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        value = write_back(cls, saved, value)
        self._write_json(path, value)
        return value

    def _write_record(self, path: Path, record: Any) -> dict[str, Any]:
        return self._write_mapping(path, type(record), asdict(record))

    def _run_value(self, run: "Run") -> dict[str, Any]:
        """A run's fields merged over the unknown fields its file holds on disk."""
        try:
            saved = json.loads((self.root / "runs" / f"{run.number}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        return write_back(type(run), saved, asdict(run))

    def _write_run(self, run: "Run", value: dict[str, Any] | None = None) -> dict[str, Any]:
        """The one writer of run files; unknown fields always survive."""
        value = self._run_value(run) if value is None else value
        self._write_json(self.root / "runs" / f"{run.number}.json", value)
        return value

    def update_inspect(self, **fields: Any) -> InspectState:
        with self._state_lock():
            updated = replace(self.load_inspect(), **fields)
            self._write_record(self.root / "inspect.json", updated)
            return updated

    def start_run(
        self, number: int, *, branch: str, worktree: str | Path, pid: int | None = None,
        lane: str = "main", tier: str = "m",
    ) -> Run:
        """Create a run; pid attests self or a newly spawned, unreaped child."""
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
            from foragent.processes import observe_tree

            identities = observe_tree(roots=(pid,)) if pid is not None else []
            run = Run(number, branch, str(worktree), pid, timestamp, timestamp, attempts=attempts,
                      restarts=restarts, lane=lane, tier=previous.tier if resumed else tier,
                      attempt_tiers=previous.attempt_tiers if resumed else {},
                      approval_body_sha=approval.body_sha if approval is not None else None,
                      last_activity_at=timestamp, process_identities=identities, **session)
            self._write_run(run)
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
            value = json.loads(path.read_text(encoding="utf-8"))
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
            from foragent.processes import observe_tree

            # Numeric fields describe a run; only birth evidence supplied by a
            # launcher (or descendants of verified identities) proves ownership.
            # An explicit new birth may reuse an earlier executor's PID.
            identities = {item["pid"]: item for item in (
                *run.process_identities, *fields.get("process_identities", []),
            )}
            fields["process_identities"] = observe_tree(list(identities.values()))
            updated = replace(run, **fields)
            self._write_run(updated)
            return updated

    def get_run(self, number: int) -> Run | None:
        try:
            value = (self.root / "runs" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return load_record(Run, json.loads(value))

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

    def pause_executor(self, executor: str, until: datetime) -> None:
        """Hold every lane that needs ``executor`` until its usage window resets."""
        with self._state_lock():
            path = self.root / "executor-pauses.json"
            pauses = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            timestamp = until.astimezone(UTC).isoformat()
            current = pauses.get(executor)
            if current is None or datetime.fromisoformat(current) < until:
                pauses[executor] = timestamp
            self._write_json(path, pauses)

    def executor_pause_until(self, executor: str, now: datetime) -> datetime | None:
        """When a usage-limit pause of ``executor`` ends, or None when it is not paused."""
        try:
            pauses = json.loads((self.root / "executor-pauses.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        until = datetime.fromisoformat(pauses[executor]) if executor in pauses else None
        return until if until is not None and now < until else None

    def executor_paused(self, executor: str, now: datetime) -> bool:
        return self.executor_pause_until(executor, now) is not None

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
            from foragent.processes import observe_tree

            finished = replace(run, status="finished", outcome=outcome, detail=detail,
                               process_identities=observe_tree(run.process_identities))
            history_path = self.root / "runs.jsonl"
            history = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
            snapshot = self._run_value(finished)
            self._atomic_write(history_path, history + json.dumps(snapshot, ensure_ascii=False) + "\n")
            self._write_run(finished, snapshot)
            return finished

    def read_history(self) -> list[Run]:
        """Return each execution once, retaining its latest completion entry."""
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        runs: dict[tuple[int, str], Run] = {}
        for line in history.splitlines():
            run = load_record(Run, json.loads(line))
            runs[run.number, run.started_at] = run
        return list(runs.values())

    def usage(self) -> dict[str, Any]:
        """The latest recorded plan usage per executor, as executors reported it."""
        try:
            return json.loads((self.root / "usage.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def record_usage(self, executor: str, windows: list[dict[str, Any]]) -> None:
        with self._state_lock():
            usage = self.usage()
            usage[executor] = {"seen_at": self._timestamp(), "windows": windows}
            self._write_json(self.root / "usage.json", usage)

    def tidy_state(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "tidy.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def update_tidy_state(self, change: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """Apply ``change`` under the state lock; it returns whether to save."""
        with self._state_lock():
            state = self.tidy_state()
            if change(state):
                self._write_json(self.root / "tidy.json", state)
            return state

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
        """Try the tick lock without waiting; yield whether this tick may run.

        Inside ``hold_tick_lock()`` on the same thread the lock is already held,
        so it yields True without taking it again.
        """
        path = self._tick_lock_path()
        if str(path.resolve()) in _held_tick_locks():
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
        key, held = str(path.resolve()), _held_tick_locks()
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
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

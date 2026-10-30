"""Local issue snapshot, its pending GitHub writes and local label/comment facts."""

import threading as _threading
from datetime import datetime, timedelta
from typing import Any

from foragent.labels import READY, issue_lane
from foragent.state_observe import OBSERVED, apply_observation, record_result

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


def _copy(value: Any) -> Any:
    """A detached copy of parsed JSON; several times cheaper than parsing or deepcopy."""
    if type(value) is dict:
        return {key: _copy(item) for key, item in value.items()}
    if type(value) is list:
        return [_copy(item) for item in value]
    return value


# Parsed issues.json per state directory, keyed by the file version one stat shows. Shared by
# every store instance in the process; an atomic replace gives a new inode, so it is seen next read.
# The directory's identity, not its spelling, names the entry, so symlinked or relative roots share it.
# Only readers use it: a locked write always parses the current file (_issue_snapshot).
# Every value leaving the cache through a public method is copied (_copy), so no caller holds it.
_RECORDS: dict[tuple[int, int], tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_RECORDS_LOCK = _threading.Lock()
_EMPTY = {"refreshed_at": None, "issues": {}}


def _version(stat: Any) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _read_issue_file(path: Any) -> tuple[tuple[int, int, int, int], dict[str, Any]] | None:
    """Parse the file and the version of exactly the bytes parsed (fstat of the open file)."""
    try:
        with path.open(encoding="utf-8") as stream:
            version = _version(_state.os.fstat(stream.fileno()))
            return version, _state.json.loads(stream.read())
    except FileNotFoundError:
        return None


class IssueSnapshotMixin:
    def _issue_record(self) -> dict[str, Any]:
        """The parsed record of the current file version; shared, so callers must not mutate it."""
        path = self.root / "issues.json"
        try:
            directory = _state.os.stat(self.root)
        except FileNotFoundError:
            return _EMPTY
        key = (directory.st_dev, directory.st_ino)

        def current() -> dict[str, Any] | None:
            try:
                version = _version(_state.os.stat(path))
            except FileNotFoundError:
                return _EMPTY
            cached = _RECORDS.get(key)
            return cached[1] if cached is not None and cached[0] == version else None

        if (record := current()) is not None:
            return record
        # One parse per version: concurrent misses wait and reuse the first parse.
        with _RECORDS_LOCK:
            if (record := current()) is not None:
                return record
            read = _read_issue_file(path)
            if read is None:
                return _EMPTY
            _RECORDS[key] = read
            return read[1]

    def _issue_view(self) -> dict[str, Any]:
        """A detached copy of the cached record for read-only lookups."""
        return _copy(self._issue_record())

    def _issue_snapshot(self) -> dict[str, Any]:
        """The current file, parsed now: the baseline of a read-modify-write under the state lock."""
        read = _read_issue_file(self.root / "issues.json")
        return _copy(_EMPTY) if read is None else read[1]

    def local_issues(self) -> list[dict[str, Any]]:
        """Read the last observed issue facts without contacting the tracker."""
        rows = self._issue_record()["issues"].values()
        return [_copy(row) for row in sorted(rows, key=lambda row: row["number"])]

    def github_outbox(self) -> list[dict[str, Any]]:
        """Read pending GitHub writes in their persisted delivery order."""
        return _copy(self._issue_record().get("outbox", []))

    def local_board_value(self, number: int, project: str, field: str) -> Any:
        """The board value last recorded for an issue's card, copying only that value."""
        row = self._issue_record()["issues"].get(str(number)) or {}
        return _copy((row.get("board_values") or {}).get(project, {}).get(field))

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
            _state._forget_reads(row)
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
            snapshot.setdefault("outbox_base", {}).setdefault(str(number), _state.deepcopy(row))
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
            if delivered["action"] in {"add_labels", "remove_labels", "update_issue", "close_issue"}:
                record_result(snapshot, row, delivered, self._timestamp())
            self._save_local_issue(snapshot, row)

    def issues_observation(self) -> dict[str, Any]:
        """When the snapshot was last read and whether a later read failed."""
        snapshot = self._issue_record()
        failure = snapshot.get("refresh_failure") or {}
        return _copy({"as_of": snapshot["refreshed_at"], "error": failure.get("error"),
                      "failed_at": failure.get("failed_at")})

    def record_issues_refresh_failure(self, error: str, failed_at: datetime) -> None:
        """Keep the last known facts and note that a later read failed."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            snapshot["refresh_failure"] = {"error": error, "failed_at": failed_at.isoformat()}
            self._write_json(self.root / "issues.json", snapshot)

    def issues_revision(self) -> int:
        return _copy(self._issue_record().get("revision", 0))

    def _save_local_issue(self, snapshot: dict[str, Any], row: dict[str, Any]) -> None:
        snapshot["revision"] = snapshot.get("revision", 0) + 1
        row.update(revision=snapshot["revision"], updated_at=self._timestamp())
        self._write_json(self.root / "issues.json", snapshot)

    def issues_refresh_due(self, now: datetime) -> bool:
        refreshed = self._issue_record()["refreshed_at"]
        return refreshed is None or now - _state.datetime.fromisoformat(refreshed) >= _state.REFRESH_INTERVAL

    def update_local_issue(self, number: int, **fields: Any) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            if any(row.get(key) != value for key, value in fields.items()):
                _state._forget_reads(row)
            # Issue facts recorded here are results GitHub accepted: an overlay, not an observation.
            seen = {key: value for key, value in fields.items() if key in OBSERVED and key != "observed_at"}
            if seen:
                record_result(snapshot, row, {"action": "facts", "fields": seen}, self._timestamp())
            row.update({key: value for key, value in fields.items() if key not in seen})
            self._save_local_issue(snapshot, row)

    def record_issue(self, issue: Any, lanes: dict, *, observed_at: datetime | None = None) -> None:
        """Apply a single read; ``observed_at`` is when the read began (default: now)."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            if issue.is_pull_request:
                return
            row = self._local_issue(snapshot, issue.number)
            facts = {"title": issue.title, "body": issue.body, "labels": sorted(issue.labels), "state": issue.state,
                     "state_reason": getattr(issue, "state_reason", None), "lane": issue_lane(issue.labels, lanes)}
            # An older observation changes nothing, not even this row's revision or update time.
            if apply_observation(snapshot, row, facts, observed_at or self._now(), lanes=lanes)[0]:
                self._save_local_issue(snapshot, row)

    def record_labels(self, number: int, labels: list[str], *, remove: bool, lanes: dict) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            # GitHub accepted the label change: an overlay until an observation shows it.
            before = row.get("labels")
            record_result(snapshot, row, {"action": "remove_labels" if remove else "add_labels",
                                          "labels": list(labels)}, self._timestamp(), lanes)
            if row.get("labels") != before:
                _state._forget_reads(row)
            if READY in labels:
                approval = self.load_approval(number)
                row["ready_approval"] = None if remove or approval is None else approval.approved_at
            self._save_local_issue(snapshot, row)

    def _record_labels(self, row: dict[str, Any], labels: list[str], *, remove: bool, lanes: dict) -> None:

        current = set(row["labels"])
        row["labels"] = sorted(current - set(labels) if remove else current | set(labels))
        if row["labels"] != sorted(current):
            _state._forget_reads(row)
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


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

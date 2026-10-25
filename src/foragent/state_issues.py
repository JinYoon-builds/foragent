"""Local issue snapshot, its pending GitHub writes and local label/comment facts."""

from datetime import datetime, timedelta
from typing import Any

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


class IssueSnapshotMixin:
    def _issue_snapshot(self) -> dict[str, Any]:
        try:
            return _state.json.loads((self.root / "issues.json").read_text(encoding="utf-8"))
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
        return refreshed is None or now - _state.datetime.fromisoformat(refreshed) >= _state.REFRESH_INTERVAL

    def update_local_issue(self, number: int, **fields: Any) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = self._local_issue(snapshot, number)
            if any(row.get(key) != value for key, value in fields.items()):
                _state._forget_reads(row)
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
                _state._forget_reads(row)
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

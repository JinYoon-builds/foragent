"""Local issue snapshot, its pending GitHub writes and local label/comment facts."""

import threading as _threading
from datetime import datetime, timedelta
from typing import Any

import foragent.state_observations as _observations


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
        """The visible issues: each latest observation plus its unretired intents, without the tracker."""
        record = self._issue_record()
        return [self._visible(record, row) for row in sorted(record["issues"].values(), key=lambda row: row["number"])]

    def github_outbox(self) -> list[dict[str, Any]]:
        """Pending GitHub writes, derived from the intent log, in queue order."""
        return self._pending(self._issue_record())

    def github_write_issues(self) -> set[int]:
        """Issues with unretired writes, including sends awaiting their next observation."""
        return {entry["issue"] for entry in self._issue_record().get("outbox", [])
                if not _observations.abandoned(entry)}

    def local_board_value(self, number: int, project: str, field: str) -> Any:
        """The board value last recorded for an issue's card, copying only that value."""
        row = self._issue_record()["issues"].get(str(number)) or {}
        return _copy((row.get("board_values") or {}).get(project, {}).get(field))

    def drop_missing_github_issue(self, number: int) -> None:
        """Observe a confirmed missing issue now; its pending writes go with it."""
        self.ingest_absence(number, self._now())

    def _local_issue(self, snapshot: dict[str, Any], number: int) -> dict[str, Any]:
        return snapshot["issues"].setdefault(str(number), {
            "number": number, "title": "", "body": "", "labels": [], "lane": "main",
            "state": "open", "pr_number": None, "merged": False, "comments": [],
        })

    def _apply_github_write(self, row: dict[str, Any], entry: dict[str, Any], lanes: dict) -> None:
        """Apply an observed-field intent to the derived view; comments never enter it."""
        action = entry["action"]
        if action != "comment":
            _state._forget_reads(row)
        if action in {"add_labels", "remove_labels"}:
            self._record_labels(row, entry["labels"], remove=action == "remove_labels", lanes=lanes,
                                ready_approval=entry.get("ready_approval", _observations._UNSET))
        elif action == "update_issue":
            row.update(title=entry["title"], body=entry["body"])
        elif action == "close_issue":
            row["state"] = "closed"

    def queue_github_write(self, number: int, action: str, *, lanes: dict | None = None,
                           started_at: datetime | None = None, premise: dict[str, Any] | None = None,
                           dropped: list[bool] | None = None, ready_approval: Any = _observations._UNSET,
                           **payload: Any) -> dict[str, Any]:
        """Append one intent to the log.

        ``queued_at`` is when the write began (``started_at``, default now).
        Its premise is the visible issue then (``premise``, default the
        visible issue now). A write queued late is judged at once, by the same
        rule as any read, against the issue's latest observation (facts or
        absence) if that read started after it; ``dropped`` receives whether
        that dropped it unsent. An issue observed missing gets no placeholder row.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            identity = snapshot.get("outbox_sequence", 0) + 1
            when = self._now() if started_at is None else started_at
            row = snapshot["issues"].get(str(number))
            if premise is None and row is not None:
                premise = _observations.observed_facts(self._visible(snapshot, row))
            entry = {"id": identity, "issue": number, "action": action,
                     "queued_at": when.astimezone(_state.UTC).isoformat(), **payload}
            from foragent.labels import READY

            record = {**entry, "premise": premise}
            if action != "comment":
                record.update(attempts=[], lanes=sorted(lanes or {}))
            if action in ("add_labels", "remove_labels") and READY in payload.get("labels", ()):
                # The approval this ready-label write belongs to, fixed when the write started.
                record["ready_approval"] = self._ready_approval(number, action == "remove_labels", ready_approval)
            snapshot.setdefault("outbox", []).append(record)
            snapshot["outbox_sequence"] = identity
            facts, seen = _observations.latest(snapshot, number)
            # Only a read that started after the write judges it now; one stored at the same
            # instant was ingested before the write began.
            lost = started_at is not None and seen is not None and seen > started_at and self._retire(
                snapshot, number, facts, seen)
            if dropped is not None:
                dropped.append(bool(lost))
            if action != "comment" and not _observations.tombstoned(snapshot, number, when) and any(
                    intent["issue"] == number for intent in snapshot["outbox"]):
                row = self._local_issue(snapshot, number)
            self._save_rows(snapshot, () if action == "comment" else (number,))
            return entry

    def acknowledge_github_comment(self, entry: dict[str, Any], identity: int) -> None:
        """A send acknowledgement removes a send-only entry and records the returned comment id."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            snapshot["outbox"] = [item for item in snapshot.get("outbox", []) if item["id"] != entry["id"]]
            row = snapshot["issues"].get(str(entry["issue"]))
            if row is not None:
                self._append_comment(row, {"id": identity, "body": entry["body"], "author": "foragent",
                                           "created_at": self._timestamp()})
            self._save_rows(snapshot, (entry["issue"],))

    @staticmethod
    def _append_comment(row: dict[str, Any], comment: dict[str, Any]) -> None:
        if all(item["id"] != comment["id"] for item in row.get("comments", [])):
            row.setdefault("comments", []).append(comment)

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

    def update_local_issue(self, number: int, *, observed_at: datetime | None = None, **fields: Any) -> None:
        """Record local fields. Issue facts among them are an observation at its time (default now)."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            if _observations.tombstoned(snapshot, number, observed_at or self._now()):
                return  # The issue was observed missing; a local change does not bring it back.
            row = self._local_issue(snapshot, number)
            facts = {key: fields.pop(key) for key in _observations.FACTS if key in fields}
            if facts:
                outcome = self._observe(snapshot, number, {**_observations.observed_facts(row), **facts},
                                        observed_at or self._now(), None, by_person=False)
                if outcome == "stale" and not fields:
                    return  # An older observation persists nothing.
                row = self._local_issue(snapshot, number)
            if any(row.get(key) != value for key, value in fields.items()):
                _state._forget_reads(row)
            row.update(fields)
            self._save_local_issue(snapshot, row)

    def record_issue(self, issue: Any, lanes: dict, *, observed_at: datetime | None = None) -> None:
        """Record an issue as an observation at its time (default now)."""
        if not issue.is_pull_request:
            self.ingest([issue], observed_at or self._now(), lanes)

    def record_labels(self, number: int, labels: list[str], *, remove: bool, lanes: dict,
                      observed_at: datetime | None = None, ready_approval: Any = _observations._UNSET) -> None:
        """A local label change, recorded as an observation at its time (default now)."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            if _observations.tombstoned(snapshot, number, observed_at or self._now()):
                return  # The issue was observed missing; a local change does not bring it back.
            row = self._local_issue(snapshot, number)
            changed = dict(row, labels=list(row["labels"]))
            self._record_labels(changed, labels, remove=remove, lanes=lanes, ready_approval=ready_approval)
            if self._observe(snapshot, number, _observations.observed_facts(changed), observed_at or self._now(),
                             lanes, by_person=False) == "stale":
                return  # An older observation persists nothing, not even its ready approval.
            row = self._local_issue(snapshot, number)
            if "ready_approval" in changed:
                row["ready_approval"] = changed["ready_approval"]
            self._save_local_issue(snapshot, row)

    def _record_labels(self, row: dict[str, Any], labels: list[str], *, remove: bool, lanes: dict,
                       ready_approval: Any = _observations._UNSET) -> None:
        from foragent.labels import READY, issue_lane

        current = set(row["labels"])
        row["labels"] = sorted(current - set(labels) if remove else current | set(labels))
        if row["labels"] != sorted(current):
            _state._forget_reads(row)
        row["lane"] = issue_lane(row["labels"], lanes)
        if READY in labels:
            row["ready_approval"] = self._ready_approval(row["number"], remove, ready_approval)

    def record_comments(self, number: int, comments: list[dict], *, append: bool = False) -> None:
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is not None:
                if append:
                    for comment in comments:
                        self._append_comment(row, comment)
                else:
                    row["comments"] = comments
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

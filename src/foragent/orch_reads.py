"""Reuse per-issue GitHub reads while a newer bulk refresh shows the issue unchanged."""

from dataclasses import asdict
from typing import Any

from foragent.tracker import Dependency, LabelEvent, pr_issue_number


FACT_KEYS = ("title", "body", "labels", "state", "remote_updated_at", "pr_number", "pr_head", "merged",
             "blocked_by", "parent")


def _facts(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {key: row.get(key) for key in FACT_KEYS}


class IssueReads:
    """Tracker reads for one tick, answered from earlier reads when facts allow.

    An earlier read is reused only in a tick that ran a bulk refresh, when a
    refresh finished after the read and left the issue and its blockers exactly
    as they were, including Foragent's own recorded writes. Otherwise the read
    goes to GitHub.
    """

    def __init__(self, tracker, store, *, refreshed: bool):
        self._tracker = tracker
        self._store = store
        # Without a refresh in this tick nothing new is known about GitHub,
        # so reads stay live and a re-approval is seen at once.
        self._refreshed = refreshed
        self._sequence = store.issues_refresh_sequence()
        self._saved = store.issue_reads()
        self._entries: dict[str, dict[str, Any]] = {}

    def _row(self, number: int) -> dict[str, Any] | None:
        return next((row for row in self._store.local_issues() if row["number"] == number), None)

    def _valid(self, entry: dict[str, Any] | None, number: int) -> bool:
        if not self._refreshed or entry is None or entry.get("refresh", self._sequence) >= self._sequence:
            return False
        if entry.get("facts") != _facts(self._row(number)):
            return False
        return all(facts == _facts(self._row(int(blocker))) for blocker, facts in entry.get("blockers", {}).items())

    def _entry(self, number: int) -> dict[str, Any]:
        key = str(number)
        if key not in self._entries:
            saved = self._saved.get(key)
            self._entries[key] = saved if self._valid(saved, number) else {
                "refresh": self._sequence, "facts": _facts(self._row(number)), "blockers": {},
            }
        return self._entries[key]

    def label_events(self, number: int) -> list[LabelEvent]:
        entry = self._entry(number)
        if "label_events" not in entry:
            entry["label_events"] = [asdict(event) for event in self._tracker.label_events(number)]
        return [LabelEvent(**event) for event in entry["label_events"]]

    def blocked_by(self, number: int) -> list[Dependency]:
        entry = self._entry(number)
        if "blocked_by" not in entry:
            dependencies = self._tracker.blocked_by(number)
            entry["blocked_by"] = [asdict(dependency) for dependency in dependencies]
            entry["blockers"] = {str(dep.number): _facts(self._row(dep.number)) for dep in dependencies}
        return [Dependency(**dependency) for dependency in entry["blocked_by"]]

    def find_pr(self, head: str) -> int | None:
        number = pr_issue_number(head)
        if number is None or head != f"foragent/issue-{number}":
            return self._tracker.find_pr(head)
        entry = self._entry(number)
        if "find_pr" not in entry:
            entry["find_pr"] = self._tracker.find_pr(head)
        return entry["find_pr"]

    def save(self) -> None:
        """Keep entries for issues still in the snapshot; this tick's reads replace older ones."""
        known = {str(row["number"]) for row in self._store.local_issues()}
        self._store.save_issue_reads({key: entry for key, entry in {**self._saved, **self._entries}.items()
                                      if key in known})

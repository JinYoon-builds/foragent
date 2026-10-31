"""Ordered ingest of observed GitHub issue facts, with local writes as overlays on top.

Every observation carries the time its read started. A row keeps the start of
the observation it shows (``observed_at``); a missing issue keeps the start of
the read that found it missing (``absent``). A strictly older observation
changes nothing; one at the same time or later replaces every observed field
together. Pending writes (the outbox) sit on top of the observed facts: an
observation at or after a write's start retires it when it shows the write's
effect, and drops it when it contradicts the write's baseline.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any

LABEL_WRITES = {"add_labels", "remove_labels"}
# Issue fields an observation shows; a local change to them is an overlay.
OBSERVED_FIELDS = ("title", "body", "labels", "state", "state_reason")


def stamp_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def newer(stamp: Any, at: datetime) -> bool:
    """Whether a stored stamp is strictly after ``at``; equal times let the later ingest win."""
    seen = stamp_time(stamp)
    return seen is not None and seen > at


def confirms(entry: dict[str, Any], issue: Any) -> bool:
    """The observation shows the write's effect (comments are never visible in issue facts)."""
    action = entry["action"]
    if action == "add_labels":
        return set(entry["labels"]) <= set(issue.labels)
    if action == "remove_labels":
        return not set(entry["labels"]) & set(issue.labels)
    if action == "update_issue":
        return (issue.title, issue.body) == (entry["title"], entry["body"])
    if action == "close_issue":
        return entry.get("body") is None and issue.state == "closed"
    return False


def contradicts(entry: dict[str, Any], issue: Any, base: dict[str, Any] | None) -> bool:
    """Someone else changed what this write depends on since it was queued.

    Labels and open/closed state conflict with every write; title and body
    only with a title or body edit (discussion content is independent).
    """
    if base is None:
        return False
    if (sorted(issue.labels), issue.state) != (base.get("labels"), base.get("state")):
        return True
    return entry["action"] == "update_issue" and (issue.title, issue.body) != (base.get("title"), base.get("body"))


def touches(entry: dict[str, Any]) -> set[str]:
    """The issue fields a write changes; writes on different fields are independent."""
    action = entry["action"]
    if action in LABEL_WRITES:
        return {f"label:{label}" for label in entry["labels"]}
    if action == "update_issue":
        return {"title", "body"}
    return {"state"} if action == "close_issue" else set()


def after(stamp: Any, revision: Any, at: datetime, since_revision: int | None) -> bool:
    """Whether something started after a read that began at ``at``.

    Only start times are compared. Equal clocks (a frozen test clock) are
    broken by store revision: saved after the read's start revision
    (``since_revision``, bulk refresh only) means it began after the read.
    """
    seen = stamp_time(stamp)
    return seen is not None and (seen > at or seen == at and since_revision is not None
                                 and (revision or 0) > since_revision)


def seen_after(row: dict[str, Any] | None, at: datetime, since_revision: int | None) -> bool:
    """A row whose observation started after a read that began at ``at``."""
    return row is not None and after(row.get("observed_at"), row.get("observed_revision"), at, since_revision)


def missing_since(snapshot: dict[str, Any], key: str) -> datetime | None:
    """When the issue was last seen missing: its absence record or the latest
    complete listing, unless that listing or a later read observed the row.

    A row made only by local writes (a queued write, an acknowledgment, a
    local field) was never observed, so it does not hide the listing's omission.
    """
    times = [stamp_time(snapshot.get("absent", {}).get(key))]
    listed = stamp_time(snapshot.get("complete_at"))
    seen = stamp_time((snapshot["issues"].get(key) or {}).get("observed_at"))
    if listed is not None and (seen is None or seen < listed):
        times.append(listed)
    times = [time for time in times if time is not None]
    return max(times) if times else None


def missing_after(snapshot: dict[str, Any], key: str, at: datetime) -> bool:
    missing = missing_since(snapshot, key)
    return missing is not None and missing > at


def judged_since(snapshot: dict[str, Any], number: int, started_at: datetime) -> bool:
    """An observation or absence newer than a write's start has already judged that write."""
    key = str(number)
    return newer((snapshot["issues"].get(key) or {}).get("observed_at"), started_at) or missing_after(
        snapshot, key, started_at)


def advance_complete_listing(snapshot: dict[str, Any], at: datetime) -> None:
    """A complete listing started at ``at`` shows every issue it omits missing, known locally or not."""
    if not newer(snapshot.get("complete_at"), at):
        snapshot["complete_at"] = at.isoformat()


def advance_absence(snapshot: dict[str, Any], key: str, at: datetime) -> None:
    absent = snapshot.setdefault("absent", {})
    if not newer(absent.get(key), at):
        absent[key] = at.isoformat()


class IssueObservationMixin:
    def _judge_pending(self, snapshot: dict[str, Any], number: int, issue: Any, at: datetime,
                       since_revision: int | None = None) -> tuple[list, list]:
        """Pending writes this observation retires (confirmed) and drops (contradicted).

        ``issue`` is what the observation shows plus the delivered writes
        that started after it, which it could not see. Only writes started
        at or before ``at`` are judged. A confirmed write
        retires unless an earlier pending write on the same field still waits
        (add then remove of one label). Retired effects advance the baseline
        before contradictions are checked against it.
        """
        pending = [entry for entry in snapshot.get("outbox", []) if entry["issue"] == number]
        judged = [entry for entry in pending
                  if not after(entry["queued_at"], entry.get("revision"), at, since_revision)]
        retired, waiting = [], set()
        for entry in judged:
            if confirms(entry, issue) and not touches(entry) & waiting:
                retired.append(entry)
            else:
                waiting |= touches(entry)
        base = snapshot.setdefault("outbox_base", {}).get(str(number))
        if base is not None:
            for entry in retired:
                self._apply_github_write(base, entry, {})
        contradicted = [entry for entry in judged
                        if entry not in retired and contradicts(entry, issue, base)]
        return retired, contradicted

    def _ingest_observation(self, snapshot: dict[str, Any], old: dict[str, Any], issue: Any, at: datetime,
                            lanes: dict, *, comments: list[dict] | None = None,
                            since_revision: int | None = None, **fields: Any) -> tuple[dict[str, Any], bool]:
        """Observed facts with the local overlays this observation leaves live on top.

        Pending writes it confirms or contradicts leave ``snapshot``'s outbox;
        delivered direct writes started at or before it are judged by it and
        leave the overlays. ``since_revision`` (the store revision when the
        read began) only breaks equal clock times; see ``after``.
        Returns the row and whether writes were dropped.
        """
        from foragent.labels import issue_lane

        number, key = issue.number, str(issue.number)
        overlays = snapshot.get("overlays", [])
        live = [item for item in overlays if item["issue"] == number
                and after(item["started_at"], item.get("revision"), at, since_revision)]
        snapshot["overlays"] = [item for item in overlays if item["issue"] != number] + live
        pending = [entry for entry in snapshot.get("outbox", []) if entry["issue"] == number]
        queued = {-entry["id"] for entry in pending}
        facts = {"title": issue.title, "body": issue.body, "labels": sorted(issue.labels),
                 "state": issue.state, "state_reason": getattr(issue, "state_reason", None)}
        row = {**old, "number": number, **facts, "lane": issue_lane(issue.labels, lanes),
               "comments": comments if comments is not None else [
                   comment for comment in old.get("comments", []) if comment["id"] not in queued],
               "observed_at": at.isoformat(), "updated_at": self._timestamp(), **fields}
        if any(old.get(name) != value for name, value in facts.items()):
            _state._forget_reads(row)
        for item in live:
            self._apply_overlay(row, item, lanes)
        # Judge pending writes against what this read saw plus later deliveries it could not see.
        seen = SimpleNamespace(title=row["title"], body=row["body"], labels=frozenset(row["labels"]),
                               state=row["state"])
        retired, contradicted = self._judge_pending(snapshot, number, seen, at, since_revision)
        gone = {entry["id"] for entry in retired + contradicted}
        snapshot["outbox"] = [entry for entry in snapshot.get("outbox", []) if entry["id"] not in gone]
        remaining = [entry for entry in pending if entry["id"] not in gone]
        bases = snapshot.setdefault("outbox_base", {})
        if not remaining:
            bases.pop(key, None)
        elif key in bases:
            # Writes still pending are next judged against what this read saw, not
            # against the facts when the first of them was queued.
            bases[key].update(title=seen.title, body=seen.body, labels=sorted(seen.labels), state=seen.state)
        for entry in remaining:
            self._apply_github_write(row, entry, lanes)
        return row, bool(contradicted)

    def _apply_overlay(self, row: dict[str, Any], item: dict[str, Any], lanes: dict) -> None:
        from foragent.labels import issue_lane

        if item["action"] != "set_fields":
            self._apply_github_write(row, item, lanes)
            return
        if any(row.get(key) != value for key, value in item["fields"].items()):
            _state._forget_reads(row)
        row.update(item["fields"])
        if "labels" in item["fields"]:
            row["lane"] = issue_lane(row["labels"], lanes)

    def _add_overlay(self, snapshot: dict[str, Any], number: int, action: str, started_at: str,
                     **payload: Any) -> dict[str, Any]:
        """A local change to observed facts, live until an observation after it.

        Its revision is the one the following save gives the row.
        """
        overlay = {"issue": number, "action": action, "started_at": started_at,
                   "revision": snapshot.get("revision", 0) + 1, **payload}
        snapshot.setdefault("overlays", []).append(overlay)
        return overlay

    def observe_issues(self, issues: list, observed_at: datetime, lanes: dict, *,
                       complete: bool = False) -> list[int]:
        """Ingest issues one read showed, its start being ``observed_at``.

        A complete listing (every issue, any state) also shows each omitted
        issue missing at ``observed_at``. Returns issues whose pending writes this dropped.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            rows, absent = snapshot["issues"], snapshot.setdefault("absent", {})
            dropped, seen, changed = set(), set(), False
            for issue in issues:
                if issue.is_pull_request:
                    continue
                key = str(issue.number)
                seen.add(key)
                old = rows.get(key)
                if old is not None and newer(old.get("observed_at"), observed_at) or missing_after(
                        snapshot, key, observed_at):
                    continue
                absent.pop(key, None)
                rows[key], contradicted = self._ingest_observation(
                    snapshot, old or self._local_issue(snapshot, issue.number), issue, observed_at, lanes)
                # The revision this observation is saved under orders it against a bulk refresh in flight.
                rows[key]["observed_revision"] = snapshot.get("revision", 0) + 1
                changed = True
                if contradicted:
                    dropped.add(issue.number)
            if complete:
                advance_complete_listing(snapshot, observed_at)
                changed = True
                for key in {*rows, *absent} - seen:
                    if not newer((rows.get(key) or {}).get("observed_at"), observed_at):
                        if self._forget_missing(snapshot, int(key), observed_at):
                            dropped.add(int(key))
                        changed = True
            if changed:
                snapshot["revision"] = snapshot.get("revision", 0) + 1
                self._write_json(self.root / "issues.json", snapshot)
            return sorted(dropped)

    def observe_absence(self, number: int, observed_at: datetime) -> bool:
        """A read starting at ``observed_at`` found the issue missing.

        A newer observation of the issue wins. Otherwise the absence is kept
        and writes started at or before it go; the row stays only for later
        local writes. Returns whether pending writes were dropped.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is not None and newer(row.get("observed_at"), observed_at):
                return False
            dropped = self._forget_missing(snapshot, number, observed_at)
            snapshot["revision"] = snapshot.get("revision", 0) + 1
            self._write_json(self.root / "issues.json", snapshot)
            return dropped

    def _forget_missing(self, snapshot: dict[str, Any], number: int, at: datetime,
                        since_revision: int | None = None) -> bool:
        """Record the issue missing at ``at``; drop its writes started by then.

        The row stays, untouched, only while a later local write still sits
        on it. Returns whether pending writes were dropped.
        """
        key = str(number)
        advance_absence(snapshot, key, at)
        outbox = snapshot.get("outbox", [])
        later = [entry for entry in outbox if entry["issue"] == number
                 and after(entry["queued_at"], entry.get("revision"), at, since_revision)]
        snapshot["outbox"] = [entry for entry in outbox if entry["issue"] != number or entry in later]
        overlays = snapshot.get("overlays", [])
        live = [item for item in overlays if item["issue"] == number
                and after(item["started_at"], item.get("revision"), at, since_revision)]
        snapshot["overlays"] = [item for item in overlays if item["issue"] != number] + live
        if not later:
            snapshot.get("outbox_base", {}).pop(key, None)
        if not later and not live:
            snapshot["issues"].pop(key, None)
        return len(snapshot["outbox"]) != len(outbox)

    def acknowledge_github_write(self, number: int, action: str, started_at: datetime, *,
                                 lanes: dict, **payload: Any) -> None:
        """Show a delivered direct write as an overlay stamped with its start.

        An observation or absence after its start already judged it, so the
        acknowledgment changes nothing; an equal start lets the later ingest
        win. The overlay stays until an observation at or after its start.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            if judged_since(snapshot, number, started_at):
                return
            row = self._local_issue(snapshot, number)
            overlay = self._add_overlay(
                snapshot, number, action, started_at.isoformat(),
                **({"labels": list(payload["labels"])} if action in LABEL_WRITES else {}),
                **({"title": payload["title"], "body": payload["body"]} if action == "update_issue" else {}))
            self._apply_overlay(row, overlay, lanes)
            self._save_local_issue(snapshot, row)


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

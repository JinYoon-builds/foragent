"""Bulk refresh of remote issue facts and per-issue read confirmation."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from foragent.state_issues import _copy
from foragent.state_observe import (
    OBSERVED,
    apply_observation,
    confirmed,
    newer_than,
    record_removal,
)


class IssueRefreshMixin:
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
            skipped: set[str] = set()
            contradicted_issues: set[int] = set()
            observed = {issue.number for issue in issues if not issue.is_pull_request}
            # Writes for an issue this listing no longer shows are dropped, unless they
            # were queued, or the issue was observed, after the listing began: then
            # the listing is older and cannot prove the issue gone.
            missing = {entry["issue"] for entry in snapshot.get("outbox", [])
                       if entry["issue"] not in observed and not newer_than(previous.get(str(entry["issue"])), now)
                       and (
                           since_revision is None or snapshot.get("outbox_base", {}).get(
                               str(entry["issue"]), {}).get("revision", 0) <= since_revision)
                       and all((_merge_time(item.get("queued_at")) or now) <= now
                               for item in snapshot.get("outbox", []) if item["issue"] == entry["issue"])}
            for issue in issues:
                if issue.is_pull_request:
                    continue
                key = str(issue.number)
                old = previous.get(key, {})
                row = {
                    **old, "number": issue.number,
                    "pr_number": None if complete_prs else old.get("pr_number"),
                    "merged": False if complete_prs else old.get("merged", False),
                    "merged_at": None if complete_prs else old.get("merged_at"),
                    "updated_at": self._timestamp(),
                }
                queued = {-entry["id"] for entry in snapshot.get("outbox", []) if entry["issue"] == issue.number}
                applied, contradicted = apply_observation(snapshot, row, {
                    "title": issue.title, "body": issue.body, "labels": sorted(issue.labels),
                    "lane": issue_lane(issue.labels, lanes), "state": issue.state,
                    "state_reason": getattr(issue, "state_reason", None),
                }, now, lanes=lanes, local_since=since_revision, forget_reads=False)
                if not applied:
                    # An observation older than the stored one (or than the issue's removal)
                    # applies nothing; an issue without a row stays without one.
                    if old:
                        rows[key] = old
                    skipped.add(key)
                    continue
                if contradicted:
                    contradicted_issues.add(issue.number)
                still = {-entry["id"] for entry in snapshot.get("outbox", []) if entry["issue"] == issue.number}
                old_comments = old.get("comments", [])
                listed = comments.get(issue.number, [c for c in old_comments if c["id"] not in queued])
                row["comments"] = [*listed, *(c for c in old_comments if c["id"] in still and c not in listed)]
                facts[key] = _state.observed_issue_facts(issue)
                rows[key] = row
            for number, row in rows.items():
                if number in skipped:
                    continue
                # Order edges and dependencies are observations, never local intent.
                if parents is not None:
                    row["parent"] = parents.get(int(number))
                if blocked_by is not None:
                    row["blocked_by"] = blocked_by.get(int(number), [])
                if children is not None:
                    row["children"] = children.get(int(number), [])
            # Run starts per issue, so a merge seen for the first time can record
            # which recorded run it ended.
            history = self.read_history()
            starts: dict[int, set[str]] = {}
            for run in [*history, *(run for run in self.active_runs() if run.kind == "issue")]:
                starts.setdefault(run.number, set()).add(run.started_at)
            pr_facts: dict[str, dict[str, Any]] = {}
            for pr in prs:
                if str(pr_issue_number(pr.head)) not in skipped and (
                        row := rows.get(str(pr_issue_number(pr.head)))) is not None:
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
                    if pr.merged_at is not None:
                        _record_merge(row, pr, starts.get(row["number"], set()),
                                      facts[str(row["number"])].get("updated_at"))
                    pr_facts[str(pr_issue_number(pr.head))] = {
                        "pr_number": pr.number, "pr_head": pr.head, "merged": pr.merged_at is not None}
            for run in history:
                if run.outcome == "merged" and str(run.number) not in skipped and (
                        row := rows.get(str(run.number))) is not None:
                    # A gate merge's size is the issue's size when the merge is first seen.
                    sizes = dict(row.get("run_sizes") or {})
                    sizes.setdefault(run.started_at, _size_of(row.get("labels", ())))
                    row["run_sizes"] = sizes
            for number, row in rows.items():
                if number in skipped:
                    continue
                # What this listing showed, kept apart from later local intent.
                listed = (pr_facts.get(number, {"pr_number": None, "pr_head": None, "merged": False})
                          if complete_prs else {key: row.get(key) for key in ("pr_number", "pr_head", "merged")})
                row["observed"] = {**facts[number], **listed}
            if since_revision is not None:
                # A local change since the listing began keeps the row's local records;
                # its observed facts are whatever apply_observation decided by time.
                for number, row in previous.items():
                    if row.get("revision", 0) > since_revision and int(number) not in missing | contradicted_issues:
                        fresh = rows.get(number)
                        if fresh is None or number in skipped:
                            rows[number] = row
                            continue
                        merged = {key: value for key, value in row.items() if key != "unconfirmed"}
                        merged.update({key: fresh[key] for key in (*OBSERVED, "github", "unconfirmed") if key in fresh})
                        rows[number] = merged
            for number, row in previous.items():
                # An issue observed after this listing began stays: an older listing omitting it
                # proves nothing about it.
                if number not in rows and newer_than(row, now, snapshot):
                    rows[number] = row
                elif number not in rows:
                    record_removal(snapshot, int(number), now)
            failure = _merge_time((snapshot.get("refresh_failure") or {}).get("failed_at"))
            if failure is None or failure <= now:
                # Only a listing that began after a failed read clears that failure.
                snapshot.pop("refresh_failure", None)
            self._write_json(self.root / "issues.json", {
                **snapshot, "issues": rows,
                # An older listing finishing late never moves the snapshot time backward.
                "refreshed_at": max(filter(None, [snapshot.get("refreshed_at"), now.isoformat()]),
                                    key=lambda value: _merge_time(value) or now),
                "outbox": [entry for entry in snapshot.get("outbox", []) if entry["issue"] not in missing],
                "outbox_base": {number: row for number, row in snapshot.get("outbox_base", {}).items()
                                if int(number) not in missing},
            })
            return sorted(missing | contradicted_issues)

    def confirmed_local_issues(self) -> list[dict[str, Any]]:
        """Issue facts as GitHub last showed them, plus writes it accepted since; no queued intent."""
        record = self._issue_record()
        rows = sorted(record["issues"].values(), key=lambda row: row["number"])
        return [_copy(confirmed(row, record)) for row in rows]

    def merged_pr_listing(self) -> dict[str, Any] | None:
        """The stored merged-PR listing and the refresh start it covers, if any."""
        try:
            listing = _state.json.loads((self.root / "merged_prs.json").read_text(encoding="utf-8"))
            datetime.fromisoformat(listing["watermark"])
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            # Without a readable listing the next refresh lists every closed PR.
            return None
        prs = listing.get("prs")
        fields = ("number", "head", "base", "merged_at", "url")
        if not isinstance(prs, list) or not all(
                isinstance(record, dict) and all(key in record for key in fields) for record in prs):
            return None
        return listing

    def record_merged_pr_listing(self, listing: dict[str, Any]) -> None:
        with self._state_lock():
            self._write_json(self.root / "merged_prs.json", listing)

    def closed_issue_watermark(self) -> datetime | None:
        """When the last light read of closed issues began, if one was recorded."""
        try:
            data = _state.json.loads((self.root / "closed_issues.json").read_text(encoding="utf-8"))
            return datetime.fromisoformat(data["watermark"])
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            return None

    def record_closed_issue_watermark(self, at: datetime) -> None:
        with self._state_lock():
            self._write_json(self.root / "closed_issues.json", {"watermark": at.isoformat()})

    def read_baseline(self) -> dict[str, Any]:
        """Facts a per-issue read will depend on, taken before the read starts."""
        snapshot = self._issue_view()
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
        snapshot = self._issue_view()
        rows = snapshot["issues"]
        entry = (rows.get(str(number)) or {}).get("reads", {}).get(name)
        refreshed = snapshot["refreshed_at"]
        if entry is None or refreshed is None or entry["refresh"] is None:
            return None
        if _state.datetime.fromisoformat(refreshed) <= _state.datetime.fromisoformat(entry["refresh"]):
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


def _merge_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _size_of(labels: Any) -> str:
    from foragent.watch_sections.progress import size_of

    return size_of(labels)


def _record_merge(row: dict[str, Any], pr: Any, starts: set[str], updated_at: str | None) -> None:
    """Keep every merged PR of the issue, in any listing order and across reopening.

    merged_at is the newest merge. Each merge is recorded once and never changed
    afterwards: whether its issue was approved, the issue's size, and the latest
    recorded run that started before it are taken when the merge is first seen.
    Whether that merge closed the issue is taken from the first listing of the issue
    updated at or after the merge (closing advances updated_at): an issue listed
    before the PR merged stays undecided (None) instead of recording a stale "open".
    A later reopening, re-approval, relabelling or re-closing changes none of it."""
    at = _merge_time(pr.merged_at)
    latest = _merge_time(row.get("merged_at"))
    if at is not None and (latest is None or at > latest):
        row["merged_at"] = pr.merged_at
    merges = dict(row.get("merges") or {})
    entry = merges.get(str(pr.number))
    if entry is None:
        before = [(when, start) for start in starts
                  if (when := _merge_time(start)) is not None and at is not None and when <= at]
        labels = row.get("labels", ())
        entry = {
            "merged_at": pr.merged_at,
            "approved": "approved" in labels,
            "closed": None,
            "size": _size_of(labels),
            "started_at": max(before)[1] if before else None,
        }
    if entry.get("closed") is None:
        seen = _merge_time(updated_at)
        # Without an update time the listing cannot be placed before the merge.
        if seen is None or at is None or seen >= at:
            entry = {**entry, "closed": row.get("state") == "closed"}
    merges[str(pr.number)] = entry
    row["merges"] = merges


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

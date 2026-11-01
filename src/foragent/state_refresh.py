"""Bulk refresh of remote issue facts and per-issue read confirmation."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any


CHILD_READ = "sub_issues"


class IssueRefreshMixin:
    def refresh_local_issues(
        self, issues: list, prs: list, lanes: dict, now: datetime,
        *, comments: dict[int, list[dict]] | None = None, since_revision: int | None = None,
        parents: dict[int, int] | None = None, blocked_by: dict[int, list[dict]] | None = None,
        children: dict[int, list[int]] | None = None, complete_prs: bool = False,
        observed_at: datetime | None = None,
    ) -> list[int]:
        """Ingest one complete listing that started at ``observed_at`` (default ``now``).

        Each issue goes through the one observation ingest: a row whose stored
        observation started later keeps everything (facts, edges, PRs,
        comments). An omitted issue is observed absent unless seen later.
        ``since_revision`` (the revision when the refresh began) only breaks
        equal start times, and keeps the PR and comment records of a row
        written during the refresh. Returns issues whose intents were dropped.
        """
        from foragent.state_observations import EARLIEST, issue_facts, stamp
        from foragent.tracker import pr_issue_number

        at = observed_at or now
        comments = comments or {}
        with self._state_lock():
            snapshot = self._issue_snapshot()
            previous = dict(snapshot["issues"])
            if (stamp(snapshot.get("complete_at")) or EARLIEST) <= at:
                snapshot["complete_at"] = at.isoformat()
            observed = {issue.number for issue in issues if not issue.is_pull_request}
            dropped, facts, fresh, local = set(), {}, set(), set()
            for issue in issues:
                if issue.is_pull_request:
                    continue
                key, old = str(issue.number), previous.get(str(issue.number)) or {}
                outcome = self._observe(snapshot, issue.number, issue_facts(issue), at, lanes,
                                        since_revision=since_revision)
                if outcome == "stale":
                    continue
                if outcome == "dropped":
                    dropped.add(issue.number)
                fresh.add(key)
                facts[key] = _state.observed_issue_facts(issue)
                row = snapshot["issues"][key]
                if since_revision is not None and old.get("revision", 0) > since_revision:
                    local.add(key)  # Written during this refresh: keep its PR and comment records.
                    continue
                if complete_prs:
                    row.update(pr_number=None, merged=False, merged_at=None)
                if issue.number in comments:
                    row["comments"] = comments[issue.number]
            from foragent.state_observations import known_numbers

            for number in sorted(known_numbers(snapshot) - observed):
                if self._observe(snapshot, number, None, at, lanes, since_revision=since_revision) == "dropped":
                    dropped.add(number)
            rows = snapshot["issues"]
            for number in fresh:
                row = rows[number]
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
                key = str(pr_issue_number(pr.head))
                if key in fresh and key not in local:
                    row = rows[key]
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
                    if pr.merged_at is not None:
                        _record_merge(row, pr, starts.get(row["number"], set()), facts[key].get("updated_at"))
                    pr_facts[key] = {"pr_number": pr.number, "pr_head": pr.head, "merged": pr.merged_at is not None}
            for run in history:
                if run.outcome == "merged" and str(run.number) in fresh:
                    # A gate merge's size is the issue's size when the merge is first seen.
                    row = rows[str(run.number)]
                    sizes = dict(row.get("run_sizes") or {})
                    sizes.setdefault(run.started_at, _size_of(row.get("labels", ())))
                    row["run_sizes"] = sizes
            for number in fresh:
                row = rows[number]
                # What this listing showed, kept apart from later local intent.
                pr_keys = ("pr_number", "pr_head", "merged")
                if number in local:
                    listed = {key: (previous[number].get("observed") or {}).get(key) for key in pr_keys}
                elif complete_prs:
                    listed = pr_facts.get(number, {"pr_number": None, "pr_head": None, "merged": False})
                else:
                    listed = {key: row.get(key) for key in pr_keys}
                row["observed"] = {**facts[number], **listed}
                row["updated_at"] = self._timestamp()
            snapshot.pop("refresh_failure", None)
            last = stamp(snapshot.get("refreshed_at"))
            snapshot["refreshed_at"] = (now if last is None or now >= last else last).isoformat()
            self._write_json(self.root / "issues.json", snapshot)
            return sorted(dropped)

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


    def record_child_read(self, number: int, children: list[dict[str, Any]], *,
                          started: datetime, baseline: dict[str, Any]) -> None:
        """Keep what one sub-issue read showed, so a later tick need not read it again.

        The numbers repair the parent's stored edges, unless a newer observation
        of the parent has arrived since. The facts stay as a remembered read for
        children that no listing shows; a changed parent, including its summary,
        ends that memory.
        """
        from foragent.state_observations import stamp

        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if row is None:
                return
            if (seen := stamp(row.get("observed_at"))) is not None and seen > started:
                return
            row["children"] = [child["number"] for child in children]
            row.setdefault("reads", {})[CHILD_READ] = {
                "value": children, "refresh": baseline["refresh"],
                "observed": {str(number): baseline["observed"].get(str(number))},
            }
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

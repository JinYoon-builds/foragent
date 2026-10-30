"""Bulk refresh of remote issue facts and per-issue read confirmation."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from foragent.state_issues import WRITES, _copy


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
                facts[str(issue.number)] = _state.observed_issue_facts(issue)
                remote = {
                    **old, "number": issue.number, "title": issue.title, "body": issue.body,
                    "labels": sorted(issue.labels), "lane": issue_lane(issue.labels, lanes),
                    "state": issue.state,
                    # Completion reason and observation time travel with the listing (#1025).
                    "state_reason": getattr(issue, "state_reason", None), "observed_at": now.isoformat(),
                    "pr_number": None if complete_prs else old.get("pr_number"),
                    "merged": False if complete_prs else old.get("merged", False),
                    "merged_at": None if complete_prs else old.get("merged_at"),
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
                        # Queued writes keep only the fields they set; GitHub's other facts are
                        # newer. Writes made after the listing began are kept below.
                        kept = {**remote, **{key: old[key] for key in pending_fields(pending) if key in old}}
                        if not concurrent:
                            # Nothing changed locally since the listing began: it confirms GitHub.
                            snapshot.get("outbox_base", {}).get(str(issue.number), {}).update(
                                {key: remote[key] for key in OBSERVED})
                        rows[str(issue.number)] = kept
                        continue
                    dropped.add(issue.number)
                rows[str(issue.number)] = remote
            for number, row in rows.items():
                # A listing never replaces issue facts observed after it began (a later read or refresh).
                if (seen := _merge_time((previous.get(number) or {}).get("observed_at"))) is not None and seen > now:
                    row.update({key: previous[number][key] for key in OBSERVED if key in previous[number]})
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
                if (row := rows.get(str(pr_issue_number(pr.head)))) is not None:
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
                    if pr.merged_at is not None:
                        _record_merge(row, pr, starts.get(row["number"], set()),
                                      facts[str(row["number"])].get("updated_at"))
                    pr_facts[str(pr_issue_number(pr.head))] = {
                        "pr_number": pr.number, "pr_head": pr.head, "merged": pr.merged_at is not None}
            for run in history:
                if run.outcome == "merged" and (row := rows.get(str(run.number))) is not None:
                    # A gate merge's size is the issue's size when the merge is first seen.
                    sizes = dict(row.get("run_sizes") or {})
                    sizes.setdefault(run.started_at, _size_of(row.get("labels", ())))
                    row["run_sizes"] = sizes
            for number, row in rows.items():
                # What this listing showed, kept apart from later local intent.
                listed = (pr_facts.get(number, {"pr_number": None, "pr_head": None, "merged": False})
                          if complete_prs else {key: row.get(key) for key in ("pr_number", "pr_head", "merged")})
                row["observed"] = {**facts[number], **listed}
            if since_revision is not None:
                # A field written locally since the listing began is newer than the listing;
                # every other field of the row takes what the listing showed.
                for number, row in previous.items():
                    if row.get("revision", 0) > since_revision and int(number) not in dropped:
                        rows[number] = _keep_local_writes(row, rows.get(number), since_revision)
            snapshot.pop("refresh_failure", None)
            self._write_json(self.root / "issues.json", {
                **snapshot, "refreshed_at": now.isoformat(), "issues": rows,
                "outbox": [entry for entry in snapshot.get("outbox", []) if entry["issue"] not in dropped],
                "outbox_base": {number: row for number, row in snapshot.get("outbox_base", {}).items()
                                if int(number) not in dropped},
            })
            return sorted(dropped)

    def confirmed_local_issues(self) -> list[dict[str, Any]]:
        """Issue facts without undelivered local writes.

        Only the fields a queued write sets read from its delivery baseline; every other
        fact (state, completion reason, merges, observation time) is the row's own."""
        record = self._issue_record()
        pending: dict[str, list[dict[str, Any]]] = {}
        for entry in record.get("outbox", []):
            pending.setdefault(str(entry["issue"]), []).append(entry)
        rows = []
        for key, row in record["issues"].items():
            base = record.get("outbox_base", {}).get(key)
            if key in pending and base is not None:
                row = {**row, **{field: base[field] for field in pending_fields(pending[key]) if field in base}}
            rows.append(_copy(row))
        return sorted(rows, key=lambda row: row["number"])

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


OBSERVED = ("title", "body", "labels", "lane", "state", "state_reason", "observed_at")


def pending_fields(pending: list[dict[str, Any]]) -> set[str]:
    """Row fields that queued writes set locally; an unknown write keeps every observed field."""
    return {field for entry in pending for field in WRITES.get(entry["action"], OBSERVED)}


def observe_under_pending(snapshot: dict[str, Any], row: dict[str, Any], observed: dict[str, Any],
                          pending: list[dict[str, Any]]) -> list[str]:
    """Apply a fresh read to an issue: queued writes keep their fields, the rest are observed.

    Returns the fields set, which the caller saves as written: this read is newer than any
    listing that began before it, so that listing must not overwrite them. The delivery
    baseline is GitHub as confirmed, so it takes every observed field, and its revision
    advances so such a listing treats the issue as changed concurrently."""
    newer = {key: value for key, value in observed.items() if key not in pending_fields(pending)}
    if any(row.get(key) != value for key, value in newer.items() if key != "observed_at"):
        _state._forget_reads(row)
    row.update(newer)
    if (base := snapshot.get("outbox_base", {}).get(str(row["number"]))) is not None:
        base.update(observed, revision=snapshot.get("revision", 0) + 1)
    return list(newer)


def _keep_local_writes(local: dict[str, Any], listed: dict[str, Any] | None, since: int) -> dict[str, Any]:
    """The local row with the listing's issue facts, except those written after the listing began.

    Only issue facts (OBSERVED) are merged field by field. PR facts and other records keep
    the existing rule: a local change since the listing began keeps the local row."""
    if listed is None:
        return local
    written = local.get("written", {})
    return {**local, **{field: listed[field] for field in OBSERVED
                        if field in listed and written.get(field, 0) <= since}}


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

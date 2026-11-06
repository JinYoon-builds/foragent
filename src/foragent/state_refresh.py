"""Bulk refresh of remote issue facts and per-issue read confirmation."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any


class IssueRefreshMixin:
    def planning_prs(self) -> list[dict[str, Any]]:
        """Open pm/* PRs observed by the gate; an unavailable record is empty."""
        try:
            data = _state.json.loads((self.root / "planning_prs.json").read_text(encoding="utf-8"))
            prs = data["prs"]
            if not isinstance(prs, list):
                return []
            return [row for row in prs if isinstance(row, dict)
                    and isinstance(row.get("number"), int)
                    and all(isinstance(row.get(key), str) for key in ("head", "title", "url"))
                    and row["head"].startswith("pm/")]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def record_planning_prs(self, prs: list, observed_at: datetime) -> None:
        """Replace the full successful observation, including an empty listing."""
        from foragent.state_observations import stamp

        with self._state_lock():
            path = self.root / "planning_prs.json"
            try:
                previous = stamp(_state.json.loads(path.read_text(encoding="utf-8"))["observed_at"])
            except (OSError, ValueError, TypeError, KeyError):
                previous = None
            if previous is not None and previous > observed_at:
                return
            self._write_json(path, {"observed_at": observed_at.isoformat(), "prs": [
                {"number": pr.number, "head": pr.head, "title": pr.title, "url": pr.url}
                for pr in prs if pr.head.startswith("pm/") and pr.merged_at is None
            ]})

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
        from foragent.state_observations import EARLIEST, NOT_LISTED, edges_after, history_baseline, issue_facts, stamp
        from foragent.tracker import pr_issue_number

        at = observed_at or now
        comments = comments or {}
        with self._state_lock():
            snapshot = self._issue_snapshot()
            previous = dict(snapshot["issues"])
            before = history_baseline(snapshot)
            if (stamp(snapshot.get("complete_at")) or EARLIEST) <= at:
                snapshot["complete_at"] = at.isoformat()
            observed = {issue.number for issue in issues if not issue.is_pull_request}
            dropped, facts, fresh, local = set(), {}, set(), set()
            for issue in issues:
                if issue.is_pull_request:
                    continue
                key, old = str(issue.number), previous.get(str(issue.number)) or {}
                outcome = self._observe(snapshot, issue.number, issue_facts(issue), at, lanes,
                                        since_revision=since_revision,
                                        created_at=getattr(issue, "created_at", None))
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

            gone = {}
            for number in sorted(known_numbers(snapshot) - observed):
                outcome = self._observe(snapshot, number, None, at, lanes, since_revision=since_revision)
                if outcome != "stale":
                    gone[number] = NOT_LISTED
                if outcome == "dropped":
                    dropped.add(number)
            rows = snapshot["issues"]
            # A row with a newer edge write keeps those edges, and this read stays out of its history.
            behind = {number for number in fresh if edges_after(snapshot, int(number), at, since_revision)}
            for number in fresh:
                row = rows[number]
                # Order edges and dependencies are observations, never local intent.
                if parents is not None:
                    row["parent"] = parents.get(int(number))
                if blocked_by is not None and number not in behind:
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
                number = pr_issue_number(pr.head)
                if number is None:
                    continue
                key = str(number)
                if key in fresh and key not in local:
                    row = rows[key]
                    row.update(pr_number=pr.number, pr_head=pr.head, pr_base=pr.base,
                               merged=pr.merged_at is not None)
                    pr_facts[key] = {"pr_number": pr.number, "pr_head": pr.head, "merged": pr.merged_at is not None}
                if pr.merged_at is not None:
                    # A merge stays a fact whatever this listing showed of its issue: absent, stale or
                    # written meanwhile. The cursor moves past it, so no later listing repeats it.
                    _attach_merge(snapshot, number, pr, starts.get(number, set()),
                                  updated_at=facts[key].get("updated_at") if key in fresh else None,
                                  observed=key in fresh)
            owner_facts = self.owner_merge_facts()
            for run in history:
                if self.run_merged(run, owner_facts=owner_facts) and str(run.number) in fresh:
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
            self._record_issue_states(snapshot, before, at, {int(number) for number in fresh},
                                     edges=blocked_by, gone=gone, since_revision=since_revision)
            return sorted(dropped)

    def merged_pr_watermark(self) -> datetime | None:
        """When the last merged-PR listing that reached the issue cache began, if any."""
        try:
            return datetime.fromisoformat(self._issue_snapshot()["merged_prs_watermark"])
        except (ValueError, TypeError, KeyError):
            # Without a readable cursor the next refresh lists every merged PR.
            return None

    def record_merged_pr_watermark(self, at: datetime) -> None:
        """Advance the merged-PR cursor kept beside the issue cache; tidy the old duplicate listings."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            snapshot["merged_prs_watermark"] = at.isoformat()
            self._write_json(self.root / "issues.json", snapshot)
            for name in ("merged_prs.json", "union-merge-paths.json"):
                (self.root / name).unlink(missing_ok=True)

    def merge_facts(self) -> dict[tuple[int, str], dict]:
        """GitHub merges the issue cache attributes to an execution, by (number, started_at)."""
        # Raw history rows: a malformed row links nothing and breaks no reader of merge times.
        runs = [*self._run_history(), *self._current_issue_runs()]
        return merge_facts_of(self._issue_record(), runs)  # Read-only: no detached copy needed.

    def _current_issue_runs(self) -> list[dict[str, Any]]:
        values = []
        for path in (self.root / "runs").glob("[0-9]*.json"):
            try:
                value = _state.json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                values.append(value)
        return values

    def observe_merge(self, number: int, pr: int | None, *, head: str, base: str, merged_at: str,
                      started_at: str, url: str = "") -> None:
        """Put a merge Foragent itself just made or confirmed into the issue cache.

        The entry follows the listing's rules (_record_merge): kept once, never changed. The
        execution is known here (a merge before its start is not its own), and whether the
        merge closed the issue stays undecided until a listing after the merge shows it.
        Without a cached row the merge waits beside the rows (``unlisted_merges``) until
        the issue is observed. A merge whose PR is not known yet is keyed ``run:<start>``
        until a listing shows that execution's PR."""
        from types import SimpleNamespace

        with self._state_lock():
            snapshot = self._issue_snapshot()
            row = snapshot["issues"].get(str(number))
            if pr is None and row is not None and type(row.get("pr_number")) is int:
                pr = row["pr_number"]  # The PR the cache already knows for this issue.
            name = str(pr) if pr is not None else f"{RUN_MERGE}{started_at}"
            known = (row if row is not None else {}).get("merges") or (
                snapshot.get("unlisted_merges", {}).get(str(number)) or {})
            if pr is None and any(key.isdigit() and entry.get("started_at") == started_at
                                  for key, entry in known.items()):
                return  # The execution's merge is already cached under its PR.
            observed = SimpleNamespace(number=name, head=head, base=base, merged_at=merged_at, url=url)
            if _attach_merge(snapshot, number, observed, {started_at}, execution=started_at):
                self._write_json(self.root / "issues.json", snapshot)

    def cached_merged_prs(self) -> list[dict[str, Any]]:
        """Merged PRs the issue cache already saw, as incremental-listing records."""
        records = []
        for row, merges in _issue_merges(self._issue_snapshot()):
            for number, entry in merges.items():
                if not number.isdigit():
                    continue  # An execution's merge whose PR no listing has shown yet.
                same = row.get("pr_number") == int(number)
                records.append({
                    "number": int(number),
                    "head": entry.get("head") or (same and row.get("pr_head")) or f"foragent/issue-{row['number']}",
                    "base": entry.get("base") or (same and row.get("pr_base")) or "",
                    "merged_at": entry.get("merged_at"), "updated_at": entry.get("merged_at"),
                    "url": entry.get("url") or "",
                })
        return records

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


RUN_MERGE = "run:"  # Key prefix of an execution's merge whose PR the cache has not seen.
EVIDENCE = "by_run"  # The entry's execution is Foragent's own record of the merge, not a listing's guess.


def merge_facts_of(snapshot: Any, runs: Any = ()) -> dict[tuple[int, str], dict]:
    """Each cached merge with its execution: {"pr", "merged_at"} by (number, started_at).

    A PR's merge belongs to the run that opened it (the run history's ``pr_number`` link), never
    to a later run of the issue. Without that link (a PR older than the link) the cache's recorded
    execution stands, unless that run opened a different PR. ``runs``: Run records or raw rows."""
    opener: dict[tuple[int, int], str] = {}
    opened: dict[tuple[int, str], int] = {}
    for run in runs:
        value = run if isinstance(run, dict) else {}
        number, started, pr = value.get("number"), value.get("started_at"), value.get("pr_number")
        if type(number) is int and isinstance(started, str) and type(pr) is int:
            opened[number, started] = pr
            if (number, pr) not in opener or started < opener[number, pr]:
                opener[number, pr] = started
    facts: dict[tuple[int, str], dict] = {}
    for row, merges in _issue_merges(snapshot):
        for pr, entry in merges.items():
            if not (isinstance(entry, dict) and isinstance(at := entry.get("merged_at"), str)):
                continue
            number = int(pr) if pr.isdigit() else None
            started = opener.get((row["number"], number)) if number is not None else None
            if started is None:
                started = entry.get("started_at")
                if not isinstance(started, str) or opened.get((row["number"], started), number) != number:
                    continue  # No execution, or that run's own PR is another one.
            key, when = (row["number"], started), _merge_time(at)
            known = _merge_time(facts[key]["merged_at"]) if key in facts else None
            if key not in facts or when is not None and (known is None or when > known):
                facts[key] = {"pr": int(pr) if pr.isdigit() else None, "merged_at": at}
    return facts


def _issue_merges(snapshot: Any):
    """(row, merges) once per issue: its row's merges joined with any still waiting for the row.

    One PR is one entry (``_merged_entries``), so a merge is never counted for two executions."""
    if not isinstance(snapshot, dict):
        return
    rows = snapshot.get("issues") if isinstance(snapshot.get("issues"), dict) else {}
    waiting = snapshot.get("unlisted_merges") if isinstance(snapshot.get("unlisted_merges"), dict) else {}
    for key in sorted({*rows, *waiting}, key=lambda key: (not key.isdigit(), key)):
        if not key.isdigit():
            continue
        row = rows.get(key) if isinstance(rows.get(key), dict) else {"number": int(key)}
        merges = _merged_entries(row.get("merges") if isinstance(row.get("merges"), dict) else {},
                                 waiting.get(key) if isinstance(waiting.get(key), dict) else {})
        yield {**row, "number": int(key)}, merges


def new_row(number: int) -> dict[str, Any]:
    return {"number": number, "title": "", "body": "", "labels": [], "lane": "main",
            "state": "open", "pr_number": None, "merged": False, "comments": []}


def issue_row(snapshot: dict[str, Any], number: int) -> dict[str, Any]:
    """The issue's cache row, created if missing; the one place a row is born.

    A merge cached while the issue had no row (``unlisted_merges``) moves into the row, so one
    merge never lives in two places: a later listing then extends that entry instead of guessing
    a second one."""
    key = str(number)
    row = snapshot["issues"].setdefault(key, new_row(number))
    waiting = (snapshot.get("unlisted_merges") or {}).pop(key, None)
    if waiting:
        row["merges"] = _merged_entries(row.get("merges"), waiting)
    if "unlisted_merges" in snapshot and not snapshot["unlisted_merges"]:
        snapshot.pop("unlisted_merges")
    return row


def _merged_entries(*sources: Any) -> dict[str, Any]:
    """Merge entries by key; for one PR, Foragent's execution evidence wins over a listing's guess."""
    merged: dict[str, Any] = {}
    for source in sources:
        for key, entry in (source or {}).items():
            if not isinstance(entry, dict):
                continue
            if key not in merged or entry.get(EVIDENCE) and not merged[key].get(EVIDENCE):
                merged[key] = entry
    return merged


def _attach_merge(snapshot: dict[str, Any], number: int, pr: Any, starts: set[str],
                  *, updated_at: str | None = None, observed: bool = False,
                  execution: str | None = None) -> bool:
    """Keep one merged PR in the issue's row, or beside the rows (``unlisted_merges``) without one.

    ``observed``: this listing showed the issue itself (updated at ``updated_at``), so it may decide
    whether the merge closed it; nothing else may. ``execution`` is Foragent's own evidence of the
    run that merged (the gate's or the merge owner's record): it corrects a listing's guess, which
    only knows the latest run that started before the merge, and no later guess replaces it. A merge
    before that run's start is not its own. Returns whether anything changed."""
    key = str(number)
    unlisted = snapshot.get("unlisted_merges") or {}
    if key in snapshot["issues"]:
        holder = issue_row(snapshot, number)  # Also adopts merges an older writer left waiting.
    else:
        holder = {"number": number, "labels": [], "merges": dict(unlisted.get(key) or {})}
    before = _copy_merges(holder.get("merges"))
    _record_merge(holder, pr, starts, updated_at, observed=observed)
    entry = holder["merges"][str(pr.number)]
    at, start = _merge_time(entry.get("merged_at")), _merge_time(execution)
    if execution is not None and at is not None and start is not None and start <= at:
        entry.update(started_at=execution, **{EVIDENCE: True})
        if str(pr.number).isdigit():
            holder["merges"].pop(f"{RUN_MERGE}{execution}", None)  # Its PR is known now.
    if key not in snapshot["issues"]:
        snapshot.setdefault("unlisted_merges", {})[key] = holder["merges"]
    return holder["merges"] != before


def _copy_merges(merges: Any) -> dict[str, Any]:
    return {key: dict(entry) for key, entry in (merges or {}).items()}


def _record_merge(row: dict[str, Any], pr: Any, starts: set[str], updated_at: str | None,
                  *, observed: bool = True) -> None:
    """Keep every merged PR of the issue, in any listing order and across reopening.

    merged_at is the newest merge. Each merge is recorded once and never changed
    afterwards: whether its issue was approved, the issue's size, and the latest
    recorded run that started before it are taken when the merge is first seen.
    Whether that merge closed the issue is taken from the first listing of the issue
    updated at or after the merge (closing advances updated_at): an issue listed
    before the PR merged stays undecided (None) instead of recording a stale "open".
    A later reopening, re-approval, relabelling or re-closing changes none of it.
    Without ``observed`` (no listing of the issue itself) "closed" stays as it is."""
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
    # Enough of the PR for a later incremental listing to list it again from this cache.
    entry = {**{"head": pr.head, "base": pr.base, "url": pr.url}, **entry}
    if observed and entry.get("closed") is None:
        seen = _merge_time(updated_at)
        # Without an update time the listing cannot be placed before the merge.
        if seen is None or at is None or seen >= at:
            entry = {**entry, "closed": row.get("state") == "closed"}
    merges[str(pr.number)] = entry
    if str(pr.number).isdigit() and entry.get("started_at"):
        # The listing now shows the PR of an execution's merge recorded without one.
        merges.pop(f"{RUN_MERGE}{entry['started_at']}", None)
    row["merges"] = merges


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state

"""An order parent's board column follows its sub-issues.

A parent is an open, unapproved issue with sub-issues. Its own labels say
nothing about progress, so the gate derives its column from the children on
each tick that read the issue listing, and closes it once every child closed
as completed. An approved issue with sub-issues stays ordinary work.

An order record (plan marker, no lane label) also owns every issue its plan
marker references: the map values and the issues in its acceptance (edit)
map. Edit-only records and records older than native sub-issue edges have no
sub-issues, so the close waits for those references as well (#837). A closed
reference counts as done whatever its reason; native sub-issues keep the
not_planned handling. Until a record closes, its card keeps today's handling.
"""

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent import board
from foragent.i18n import text
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, RUNNING
from foragent.orch_events import append_event
from foragent.tracker import Issue, TrackerError

log = logging.getLogger(__name__)

NOT_PLANNED_FLAG = "parent-child-not-planned"
REFERENCE_READS_FILE = "order_reference_reads.json"  # Written only here: reads of unlisted references.
CONFIRMED_FOR = timedelta(hours=1)  # How long a closed read of an unlisted reference counts.
ROTATION_SECONDS = 300  # One step of the record rotation: the listing refresh interval.
MAX_REFERENCE_READS = 20  # Extra get_issue reads one tick may spend on order-record references.
_MARKER_START = "<!-- foragent:plan "
_PLAN_MARKER = re.compile(r"<!-- foragent:plan id=\S+ map=(\{[^\n]*?\})(?: acceptance=(\{[^\n]*\}))? -->")


def _completed(child: Issue) -> bool:
    # GitHub closes as completed by default; an unknown reason is that default.
    return child.state == "closed" and getattr(child, "state_reason", None) in {None, "completed"}


def parent_state(children: Iterable[Issue], open_prs: set[int] = frozenset()) -> str | None:
    """The gate state whose board column the parent shows, or None without children."""
    children = list(children)
    if not children:
        return None
    open_children = [child for child in children if child.state == "open"]
    completed = [child for child in children if _completed(child)]
    if any(NEEDS_HUMAN in child.labels for child in open_children):
        return "Needs human"
    if len(completed) == len(children):
        return "Done"
    if completed or any(child.labels & {RUNNING, BLOCKED} or child.number in open_prs
                        for child in open_children):
        return "Running"
    if open_children and all(APPROVED in child.labels for child in open_children):
        return "Approved"
    return "unapproved"


def order_parents(rows: list[dict[str, Any]]) -> set[int]:
    """Open, unapproved issues that the last listing showed with sub-issues.

    The parent's own blocked or needs-human label does not take it out: its
    column, completion and cancellation flag still come from its children.
    """
    return {row["number"] for row in rows
            if row.get("state") == "open" and row.get("children")
            and APPROVED not in row.get("labels", ())}


def plan_references(body: str) -> set[int] | None:
    """Issues a plan marker names: its map values and its acceptance (edit) map keys.

    None when the body has a plan marker that does not parse; a marker with
    any invalid entry is unparseable as a whole, so no reference is dropped.
    """
    if _MARKER_START not in (body or ""):
        return set()
    marker = _PLAN_MARKER.search(body)
    if marker is None:
        return None
    try:
        mapping = json.loads(marker[1])
        acceptance = json.loads(marker[2]) if marker[2] else {}
    except ValueError:
        return None
    if (not isinstance(mapping, dict) or not isinstance(acceptance, dict)
            or any(type(n) is not int or n <= 0 for n in mapping.values())
            or any(not isinstance(key, str) or not key.isdecimal() or int(key) <= 0 for key in acceptance)):
        return None
    return set(mapping.values()) | {int(key) for key in acceptance}


def order_records(rows: list[dict[str, Any]]) -> tuple[dict[int, set[int]], set[int]]:
    """Open order records (plan marker, no lane label) with their references, and those whose marker does not parse.

    An approved issue is ordinary work even with a plan marker, as with sub-issues.
    A record may reference itself; it is open, so that keeps it open.
    """
    records, unparseable = {}, set()
    for row in rows:
        labels = row.get("labels", ())
        if (row.get("state") != "open" or APPROVED in labels
                or any(label.startswith("lane:") for label in labels)
                or _MARKER_START not in (body := row.get("body") or "")):
            continue
        references = plan_references(body)
        if references is None:
            unparseable.add(row["number"])
        else:
            records[row["number"]] = references
    return records, unparseable


def _parse_time(value: str) -> datetime | None:
    """A saved read time; one without a time zone is not trusted."""
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _saved_reads(store) -> dict[str, Any]:
    """What earlier ticks read about references the listing does not show.

    ``prs``: numbers that read as pull requests (a pull request stays one).
    ``confirmed``: per record, unlisted references read as closed and when.
    """
    saved: Any = {}
    if (root := getattr(store, "root", None)) is not None:
        try:
            saved = json.loads((Path(root) / REFERENCE_READS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    confirmed = saved.get("confirmed") if isinstance(saved.get("confirmed"), dict) else {}
    return {
        "prs": {n for n in saved.get("prs", ()) if type(n) is int},
        "confirmed": {int(record): {int(n): at for n, at in numbers.items()
                                    if n.isdecimal() and isinstance(at, str)}
                      for record, numbers in confirmed.items()
                      if record.isdecimal() and isinstance(numbers, dict)},
    }


def _save_reads(store, saved: dict[str, Any], records: dict[int, set[int]]) -> None:
    """Keep what open records still reference; write only when it changed."""
    root = getattr(store, "root", None)
    if root is None:
        return
    referenced = set().union(*records.values())
    value = {"prs": sorted(saved["prs"] & referenced),
             "confirmed": {str(record): {str(n): at for n, at in sorted(numbers.items())}
                           for record, numbers in sorted(saved["confirmed"].items())
                           if record in records and numbers}}
    before = _saved_reads(store)
    if value == {"prs": sorted(before["prs"]),
                 "confirmed": {str(r): {str(n): at for n, at in sorted(ns.items())}
                               for r, ns in sorted(before["confirmed"].items())}}:
        return
    try:
        fd, temporary = tempfile.mkstemp(dir=root, prefix=f".{REFERENCE_READS_FILE}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value) + "\n")
        os.replace(temporary, Path(root) / REFERENCE_READS_FILE)
    except OSError as exc:
        log.warning("Could not remember order record reference reads: %s", exc)


def _closed_references(ctx, references: set[int], rows: dict[int, dict[str, Any]],
                       known_prs: set[int], reads: dict[str, Any],
                       confirmed: dict[int, str]) -> set[int] | None:
    """The referenced issues once every one is closed, or None while any is open or unknown.

    A closed reference counts as done whatever its close reason. The listing
    answers first; an unlisted issue is read within the tick's allowance. A
    record with more unlisted issues than one tick may read keeps its closed
    reads in ``confirmed`` and continues on later ticks. Those reads count only
    while nothing of the record was seen open or unreadable since, and only for
    an hour: an open or unreadable reference clears them, so a close never
    rests on a read taken while other work was still open. A pull request
    number is ignored.
    """
    now = ctx.now()
    closed, missing = set(), []
    for number in sorted(references):
        row = rows.get(number)
        if number in known_prs:
            continue
        if row is None and number in confirmed:
            closed.add(number)
        elif row is None:
            missing.append(number)
        elif row.get("state") != "closed":
            confirmed.clear()
            return None  # Open work keeps the record open without spending reads.
        else:
            closed.add(number)
    for number in missing:
        if number not in reads["issues"]:
            if reads["left"] <= 0:
                return None  # The rest is read on later ticks; the closed reads so far are kept.
            reads["left"] -= 1
            try:
                reads["issues"][number] = ctx.tracker.get_issue(number)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                log.warning("Could not read #%s, which an order record references: %s", number, exc)
                reads["issues"][number] = None
        issue = reads["issues"][number]
        if issue is None or not issue.is_pull_request and issue.state != "closed":
            confirmed.clear()
            return None  # Unreadable or open work keeps the record open.
        if issue.is_pull_request:
            reads["prs"].add(number)
        else:
            closed.add(number)
            confirmed[number] = now.isoformat()
    return closed


def follow_children(ctx, report) -> None:
    """Move each order parent's card to its children's column; close it when all its work is done."""
    rows = ctx.store.local_issues()
    by_number = {row["number"]: row for row in rows}
    open_prs = {row["number"] for row in rows if row.get("pr_number") and not row.get("merged")}
    listed_prs = {row["pr_number"] for row in rows if row.get("pr_number")}
    saved = _saved_reads(ctx.store)
    oldest = ctx.now() - CONFIRMED_FOR
    for record in list(saved["confirmed"]):
        saved["confirmed"][record] = {n: at for n, at in saved["confirmed"][record].items()
                                      if (read_at := _parse_time(at)) is not None and oldest <= read_at}
    parents = order_parents(rows)
    records, unparseable = order_records(rows)
    reads = {"left": MAX_REFERENCE_READS, "issues": {}, "prs": saved["prs"]}
    language = ctx.config.language
    order = sorted(parents | records.keys())
    if order:  # Rotate each tick so the read allowance does not always go to the same records.
        start = int(ctx.now().timestamp() // ROTATION_SECONDS) % len(order)
        order = order[start:] + order[:start]
    try:
        for number in order:
            children = ctx.tracker.sub_issues(number) if by_number[number].get("children") else []
            # Until a record closes, only an order parent's sub-issues move its card (today's handling).
            state = parent_state(children, open_prs) if number in parents else None
            cancelled = [child.number for child in children
                         if child.state == "closed" and not _completed(child)]
            if cancelled and number in parents:
                previous = ctx.store.load_flag(number)
                if previous is None or previous.reason != NOT_PLANNED_FLAG:
                    at = ctx.now()
                    ctx.store.save_flag(number, NOT_PLANNED_FLAG, flagged_at=at.isoformat())
                    summary = text("parent_child_not_planned", language, number=number,
                                   children=", ".join(f"#{child}" for child in cancelled))
                    append_event(ctx.store, at, number, NOT_PLANNED_FLAG, summary)
                    report.actions.append(summary)
            work = {child.number for child in children}
            if number in records:
                done = None
                confirmed = saved["confirmed"].setdefault(number, {})
                if all(_completed(child) for child in children):
                    # Sub-issues already carry their state; read only the other references.
                    done = _closed_references(ctx, records[number] - work, by_number,
                                              listed_prs | reads["prs"], reads, confirmed)
                else:
                    confirmed.clear()  # Open work: earlier closed reads no longer count.
                closes = done is not None and bool(work | done)
                work |= done or set()
            elif number in unparseable:
                closes = False  # Its work is unknown: skip the close quietly; the card keeps today's column.
            else:
                closes = state == "Done"
            if closes:
                ctx.tracker.close_issue(number, text("order_record_closed", language, count=len(work)))
                report.actions.append(text("parent_closed_report", language, number=number,
                                           count=len(work)))
                state = "Done"
            if state is not None:
                board.sync_status(ctx, number, state, parent=True)
    finally:
        _save_reads(ctx.store, saved, records)

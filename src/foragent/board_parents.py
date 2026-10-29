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
import re
from collections.abc import Iterable
from typing import Any

from foragent import board
from foragent.i18n import text
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, RUNNING
from foragent.orch_events import append_event
from foragent.tracker import Issue, TrackerError

log = logging.getLogger(__name__)

NOT_PLANNED_FLAG = "parent-child-not-planned"
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


def _closed_references(ctx, references: set[int], rows: dict[int, dict[str, Any]],
                       listed_prs: set[int]) -> set[int] | None:
    """The referenced issues if every one is closed now, or None while any is open or unreadable.

    A closed reference counts as done whatever its close reason. Listed issues
    answer from the listing, and only when all of them are closed is each
    unlisted reference read fresh, in this tick; nothing is kept between
    ticks. A number that reads as a pull request is ignored.
    """
    closed, unlisted = set(), []
    for number in sorted(references - listed_prs):
        row = rows.get(number)
        if row is None:
            unlisted.append(number)
        elif row.get("state") != "closed":
            return None  # Open work keeps the record open without spending reads.
        else:
            closed.add(number)
    for number in unlisted:
        try:
            issue = ctx.tracker.get_issue(number)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            log.warning("Could not read #%s, which an order record references: %s", number, exc)
            return None
        if issue.is_pull_request:
            continue
        if issue.state != "closed":
            return None
        closed.add(number)
    return closed


def follow_children(ctx, report) -> None:
    """Move each order parent's card to its children's column; close it when all its work is done."""
    rows = ctx.store.local_issues()
    by_number = {row["number"]: row for row in rows}
    open_prs = {row["number"] for row in rows if row.get("pr_number") and not row.get("merged")}
    listed_prs = {row["pr_number"] for row in rows if row.get("pr_number")}
    parents = order_parents(rows)
    records, unparseable = order_records(rows)
    language = ctx.config.language
    for number in sorted(parents | records.keys()):
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
            if all(_completed(child) for child in children):
                # Sub-issues already carry their state; read only the other references.
                done = _closed_references(ctx, records[number] - work, by_number, listed_prs)
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

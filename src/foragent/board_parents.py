"""An order parent's board column follows its sub-issues.

A parent is an open, unapproved issue with sub-issues. Its own labels say
nothing about progress, so the gate derives its column from the children on
each tick that read the issue listing, and closes it once every child closed
as completed. An approved issue with sub-issues stays ordinary work.

An order record (plan marker, no lane label) also owns every issue its plan
marker references: the map values and the issues in its acceptance (edit)
map. Edit-only records and records older than native sub-issue edges have no
sub-issues, so the close waits for those references as well (#837). Only the
close reads them; the board column still comes from sub-issues alone.
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


def plan_references(body: str) -> set[int]:
    """Issues a plan marker names: its map values and its acceptance (edit) map keys."""
    marker = _PLAN_MARKER.search(body or "")
    if marker is None:
        return set()
    try:
        mapping = json.loads(marker[1])
        acceptance = json.loads(marker[2]) if marker[2] else {}
    except ValueError:
        return set()
    numbers = set()
    if isinstance(mapping, dict):
        numbers |= {n for n in mapping.values() if type(n) is int and n > 0}
    if isinstance(acceptance, dict):
        numbers |= {int(key) for key in acceptance if isinstance(key, str) and key.isdecimal() and int(key) > 0}
    return numbers


def order_records(rows: list[dict[str, Any]]) -> dict[int, set[int]]:
    """Open, unapproved order records (plan marker, no lane label) and the issues they reference."""
    records = {}
    for row in rows:
        labels = row.get("labels", ())
        if (row.get("state") != "open" or APPROVED in labels
                or any(label.startswith("lane:") for label in labels)):
            continue
        if references := plan_references(row.get("body", "")) - {row["number"]}:
            records[row["number"]] = references
    return records


def _referenced_work(ctx, references: set[int], known: dict[int, Issue],
                     rows: dict[int, dict[str, Any]]) -> list[Issue] | None:
    """The referenced issues besides the sub-issues, or None while any of them is open.

    The local listing gives open/closed; a closed issue's reason needs a read, so
    reads happen only once nothing is open. An issue missing locally is read.
    """
    found, closed = [], []
    for number in sorted(references - known.keys()):
        row = rows.get(number)
        if row is None:
            found.append(ctx.tracker.get_issue(number))
        elif row.get("state") == "open":
            return None
        else:
            closed.append(number)
    if any(issue.state == "open" for issue in [*known.values(), *found]):
        return None
    return found + [ctx.tracker.get_issue(number) for number in closed]


def follow_children(ctx, report) -> None:
    """Move each order parent's card to its children's column; close it when all its work is done."""
    rows = ctx.store.local_issues()
    by_number = {row["number"]: row for row in rows}
    open_prs = {row["number"] for row in rows if row.get("pr_number") and not row.get("merged")}
    records = order_records(rows)
    language = ctx.config.language
    for number in sorted(order_parents(rows) | records.keys()):
        children = ctx.tracker.sub_issues(number) if by_number[number].get("children") else []
        state = parent_state(children, open_prs)
        work = children
        if references := records.get(number, set()) - {child.number for child in children}:
            try:
                extra = _referenced_work(ctx, references, {c.number: c for c in children}, by_number)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                log.warning("Could not read the issues order record #%s references: %s", number, exc)
                extra = None
            # Open (or unreadable) referenced work keeps the record open.
            work = children + extra if extra is not None else None
        if state is None and not work:
            continue
        cancelled = [child.number for child in work or children
                     if child.state == "closed" and not _completed(child)]
        if cancelled:
            previous = ctx.store.load_flag(number)
            if previous is None or previous.reason != NOT_PLANNED_FLAG:
                at = ctx.now()
                ctx.store.save_flag(number, NOT_PLANNED_FLAG, flagged_at=at.isoformat())
                summary = text("parent_child_not_planned", language, number=number,
                               children=", ".join(f"#{child}" for child in cancelled))
                append_event(ctx.store, at, number, NOT_PLANNED_FLAG, summary)
                report.actions.append(summary)
        if work and all(_completed(issue) for issue in work):
            ctx.tracker.close_issue(number, text("order_record_closed", language, count=len(work)))
            report.actions.append(text("parent_closed_report", language, number=number,
                                       count=len(work)))
            state = "Done"
        if state is not None:
            board.sync_status(ctx, number, state, parent=True)

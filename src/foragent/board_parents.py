"""An order parent's board column follows its sub-issues.

A parent is an open, unapproved issue with sub-issues. Its own labels say
nothing about progress, so the gate derives its column from the children on
each tick that read the issue listing, and closes it once every child closed
as completed. An approved issue with sub-issues stays ordinary work.
"""

from collections.abc import Iterable
from typing import Any

from foragent import board
from foragent.i18n import text
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, RUNNING
from foragent.orch_events import append_event
from foragent.tracker import Issue

NOT_PLANNED_FLAG = "parent-child-not-planned"


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


def batch_pending(number: int, rows: list[dict[str, Any]]) -> bool:
    """Whether this parent's batch PR has yet to merge.

    In batch review each child merges into ``foragent/batch-<parent>``, so a
    completed child is not yet on the base branch. The gate creates (and
    retries) the batch PR only while the parent is open, so closing earlier
    would strand the children's work. Once the batch PR merges, this is False
    and the parent completes like any other order.
    """
    branch = f"foragent/batch-{number}"
    if any(row.get("pr_head") == branch and row.get("merged") for row in rows):
        return False
    return any(branch in (row.get("pr_base"), row.get("pr_head")) for row in rows)


def follow_children(ctx, report) -> None:
    """Move each order parent's card to its children's column; close it when all are done."""
    rows = ctx.store.local_issues()
    open_prs = {row["number"] for row in rows if row.get("pr_number") and not row.get("merged")}
    language = ctx.config.language
    for number in sorted(order_parents(rows)):
        children = ctx.tracker.sub_issues(number)
        state = parent_state(children, open_prs)
        if state is None:
            continue
        cancelled = [child.number for child in children
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
        if state == "Done" and batch_pending(number, rows):
            # Completion waits only for the batch PR to reach the base branch.
            state = "PR open"
        if state == "Done":
            ctx.tracker.close_issue(number, text("parent_children_done", language, count=len(children)))
            report.actions.append(text("parent_closed_report", language, number=number,
                                       count=len(children)))
        board.sync_status(ctx, number, state, parent=True)

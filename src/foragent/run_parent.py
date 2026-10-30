"""A published order's shared outcome and constraints for each of its children."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.state import Approval

_PLAN_MARKER = re.compile(r"<!-- foragent:plan id=.*?-->", re.DOTALL)
_CHECKLIST_LINE = re.compile(r"- \[[ xX]\] #\d+\b.*")


def parent_summary(body: str) -> str:
    """The summary section of a Foragent order parent: no checklist, no plan or owner-label marker."""
    if not _PLAN_MARKER.search(body):
        return ""
    from foragent.ownership import OWNED_MARKER

    text = _PLAN_MARKER.sub("", body)
    lines = [line for line in text.splitlines()
             if not _CHECKLIST_LINE.fullmatch(line.strip()) and line.strip() != OWNED_MARKER]
    return "\n".join(lines).strip()


def parent_context(ctx: RunContext, n: int, approval: Approval | None) -> Approval | None:
    """Freeze the parent text into the approval snapshot the first time a run reads it.

    A re-approval while the parent is read wins: its snapshot is kept and read again.
    """
    for _ in range(3):
        if approval is None or approval.parent_context is not None:
            return approval
        parent = ctx.tracker.parent_issue(n)  # one GitHub call per approval, then frozen
        text = parent_summary(parent.body) if parent is not None else ""
        current = ctx.store.freeze_parent_context(n, approval, text)
        if current is not None and current.parent_context is not None:
            return current
        approval = current
    return approval

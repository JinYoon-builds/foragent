"""A published order's shared outcome and constraints for each of its children."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.state import Approval

_PLAN_MARKER = re.compile(r"<!-- foragent:plan id=.*?-->", re.DOTALL)
_CHECKLIST_LINE = re.compile(r"- \[[ xX]\] #\d+\b.*")


def parent_summary(body: str) -> str:
    """The summary section of a Foragent order parent: no checklist, no plan marker."""
    if not _PLAN_MARKER.search(body):
        return ""
    text = _PLAN_MARKER.sub("", body)
    lines = [line for line in text.splitlines() if not _CHECKLIST_LINE.fullmatch(line.strip())]
    return "\n".join(lines).strip()


def parent_context(ctx: RunContext, n: int, approval: Approval) -> Approval:
    """Freeze the parent text into the approval snapshot the first time a run reads it."""
    if approval.parent_context is not None:
        return approval
    parent = ctx.tracker.parent_of(n)
    text = parent_summary(ctx.tracker.get_issue(parent).body) if parent is not None else ""
    ctx.store.save_approval(
        n, body=approval.body, body_sha=approval.body_sha, acceptance_blobs=approval.acceptance_blobs,
        approved_by=approval.approved_by, approved_at=approval.approved_at, parent_context=text,
    )
    return replace(approval, parent_context=text)

"""A published order's shared outcome and constraints for each of its children."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from foragent.reasons import format_contract_comment, parse_contract_parent

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
    """The approval with the parent text this build reads, bound to the run (#1184).

    A build reads the current parent once, at its start, and keeps it while the gate continues
    it (stall, crash, limit-wait). A continuing run without its saved text recovers it from the
    public contract comment of the same build and approval generation; an earlier build's contract
    never counts. A manual retry or re-approval is a new build and reads the current parent.
    The approval copy's own frozen parent text is never read.
    """
    from foragent.state_local import _approval_identity

    run = ctx.store.get_run(n)
    for _ in range(3):  # A re-approval while the parent is read wins: read again for it.
        if approval is None:
            return None
        generation = (approval.body_sha, approval.approved_at)
        bound = (run is not None and run.parent_context is not None
                 and (run.contract_sha, run.contract_approved_at) == generation)
        if bound:  # This build already read its parent for this approval: never read it again.
            text, published = run.parent_context, run.contract_published
            break
        text, published = None, False
        if run is not None and run.continued and run.build_id:  # only this build's own contract
            text = next((found for comment in reversed(ctx.tracker.list_comments(n)) if (found := parse_contract_parent(
                comment.body, approval.body_sha, approval.approved_at, run.build_id)) is not None), None)
            published = text is not None
        if text is None:
            parent = ctx.tracker.parent_issue(n)
            text = parent_summary(parent.body) if parent is not None else ""
        current = ctx.store.load_approval(n)
        if current is None or _approval_identity(current) == _approval_identity(approval):
            break
        if _ == 2:  # Re-approved on every read: keep the text with the approval it was read for.
            break
        approval = current
    if run is None:
        return replace(approval, parent_context=text)
    # The snapshot is bound to the approval the build actually uses, whatever start_run saw (snapshot first).
    build = run.build_id or run.started_at
    if not bound:
        ctx.store.update_run(n, parent_context=text, contract_sha=approval.body_sha, build_id=build,
                             contract_approved_at=approval.approved_at, contract_published=published)
    if text and not published:
        ctx.tracker.comment(n, format_contract_comment(
            n, approval.body, approval.body_sha, approval.acceptance_blobs,
            approval.approved_by, approval.approved_at, text, build,
        ))
        ctx.store.update_run(n, contract_published=True)
    return replace(approval, parent_context=text)

"""Refuse a retry that would repeat a same-class review hold.

The gate holds an issue with ``review-defects`` after two failed rounds of the same
defect class. Another implementation of the same approved contract only repeats that
failure, so `fa orch retry` refuses until an approved order edit changes the contract:
the approved issue body (Goal, Done when) or the locked acceptance test contents.

A re-approval alone is no evidence: the gate records a re-approval given during a hold
(``scheduler_approval._record_held_approval``) even when nothing changed. So the
decision compares contents, never approval times: the approval snapshot in force now
against what the held build was working to, its run's approval body hash and the locked
test blobs on the base when the hold was flagged.
"""

from __future__ import annotations

from foragent.git import GitError
from foragent.orch_stale_files import approval_base
from foragent.reasons import Reason

REFUSAL = ("#{n} is held after two failed rounds of the same defect class; another retry "
           "would repeat the same failure, with or without --hint. Narrow, split or redesign "
           "its Goal through an order edit; once the edited Goal or acceptance tests are "
           "approved, retry works again.")


def _body_changed(ctx, n: int, approval) -> bool:
    """The approved body differs from the one the held build was started for."""
    run = ctx.store.get_run(n)
    held = run.approval_body_sha if run is not None else None
    return held is not None and held != approval.body_sha


def _tests_changed(ctx, flag, approval) -> bool:
    """The approved locked test contents differ from the base's when the hold was flagged."""
    if not approval.acceptance_blobs:
        return False
    try:
        base = approval_base(ctx, f"origin/{ctx.config.base_branch}", flag.flagged_at)
        if base is None:
            return False  # Unknown: the hold stands.
        held = ctx.git.blob_shas(base, approval.acceptance_blobs)
    except (GitError, ValueError):
        return False
    return held != approval.acceptance_blobs


def refusal(ctx, n: int) -> str | None:
    """The refusal text while the same-class hold stands on an unchanged approved contract."""
    flag = ctx.store.load_flag(n)
    if flag is None or flag.reason != Reason.REVIEW_DEFECTS:
        return None
    approval = ctx.store.load_approval(n)
    if approval is not None and (_body_changed(ctx, n, approval) or _tests_changed(ctx, flag, approval)):
        return None  # An approved order edit changed what will be built.
    return REFUSAL.format(n=n)

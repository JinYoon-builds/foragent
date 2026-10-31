"""Shared issue flags and approval invalidation for tick and runner."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from foragent import board
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.orch_transition import transition_state
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import RecordingTracker, Tracker


class FlagContext(board.BoardContext, Protocol):
    store: StateStore
    tracker: Tracker
    now: Callable[[], datetime]


def flag(
    ctx: FlagContext, n: int, reason: Reason, summary: str, details: str | None = None,
    *, needs_human: bool = False, remove_labels: tuple[str, ...] = (),
) -> None:
    """Block an issue with a reason; needs_human also hands it to a person at once.

    With needs_human the issue also gets the needs-human label, and the one
    comment carries the needs-human marker, so the orchestrator timeout does
    not escalate it again.
    """
    before = transition_state(ctx, n)
    added = [BLOCKED, NEEDS_HUMAN] if needs_human else [BLOCKED]
    removed = [READY, RUNNING, *remove_labels]
    after = before.after(add=added, remove=removed, blocked_reason=(reason, summary))
    body = format_comment(reason, summary, details, label=NEEDS_HUMAN if needs_human else BLOCKED)
    run = ctx.store.get_run(n)
    if run is not None and run.status == "running":
        ctx.store.update_run(n, detail=f"{summary}\n{details}" if details else summary)
    if before != after:
        ctx.store.save_flag(n, reason, summary=summary, flagged_at=ctx.now().astimezone(UTC).isoformat())
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=getattr(ctx.tracker, "queue_writes", False))
    tracker.add_labels(n, added)
    tracker.remove_labels(n, removed)
    if before != after:
        tracker.comment(n, body)
    board.sync_status(ctx, n, "Blocked")


def invalidate_approval(
    ctx: FlagContext, n: int, summary: str, details: str | None = None,
) -> None:
    flag(ctx, n, Reason.HASH_MISMATCH, summary, details, remove_labels=(APPROVED,))
    ctx.store.clear_approval(n)

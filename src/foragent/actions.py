"""Shared issue flags and approval invalidation for tick and runner."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from foragent import board
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import Tracker


class FlagContext(board.BoardContext, Protocol):
    store: StateStore
    tracker: Tracker
    now: Callable[[], datetime]


def flag(
    ctx: FlagContext, n: int, reason: Reason, summary: str, details: str | None = None,
    *, needs_human: bool = False,
) -> None:
    """Block an issue with a reason; needs_human also hands it to a person at once.

    With needs_human the issue also gets the needs-human label, and the one
    comment carries the needs-human marker, so the orchestrator timeout does
    not escalate it again.
    """
    run = ctx.store.get_run(n)
    if run is not None and run.status == "running":
        ctx.store.update_run(n, detail=f"{summary}\n{details}" if details else summary)
    ctx.store.save_flag(n, reason, flagged_at=ctx.now().astimezone(UTC).isoformat())
    ctx.tracker.add_labels(n, [BLOCKED, NEEDS_HUMAN] if needs_human else [BLOCKED])
    ctx.tracker.remove_labels(n, [READY, RUNNING])
    board.sync_status(ctx, n, "Blocked")
    ctx.tracker.comment(n, format_comment(reason, summary, details, label=NEEDS_HUMAN if needs_human else BLOCKED))


def invalidate_approval(
    ctx: FlagContext, n: int, summary: str, details: str | None = None,
) -> None:
    flag(ctx, n, Reason.HASH_MISMATCH, summary, details)
    ctx.tracker.remove_labels(n, [APPROVED])
    ctx.store.clear_approval(n)

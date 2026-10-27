"""Shared issue flags and approval invalidation for tick and runner."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from foragent import board
from foragent.labels import APPROVED, BLOCKED, READY, RUNNING
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import Tracker


class FlagContext(board.BoardContext, Protocol):
    store: StateStore
    tracker: Tracker
    now: Callable[[], datetime]


def flag(
    ctx: FlagContext, n: int, reason: Reason, summary: str, details: str | None = None,
) -> None:
    from foragent.orch_stop import yield_to_stop

    yield_to_stop(ctx.store, n)  # A runner refuses nothing once a stop request owns its run.
    run = ctx.store.get_run(n)
    if run is not None and run.status == "running":
        ctx.store.update_run(n, detail=f"{summary}\n{details}" if details else summary)
    ctx.store.save_flag(n, reason, flagged_at=ctx.now().astimezone(UTC).isoformat())
    ctx.tracker.add_labels(n, [BLOCKED])
    ctx.tracker.remove_labels(n, [READY, RUNNING])
    board.sync_status(ctx, n, "Blocked")
    ctx.tracker.comment(n, format_comment(reason, summary, details, label=BLOCKED))


def invalidate_approval(
    ctx: FlagContext, n: int, summary: str, details: str | None = None,
) -> None:
    flag(ctx, n, Reason.HASH_MISMATCH, summary, details)
    ctx.tracker.remove_labels(n, [APPROVED])
    ctx.store.clear_approval(n)

"""Shared issue flags and approval invalidation for tick and runner."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from foragent import board
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.reasons import Reason, flag_marker, format_comment
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
    body = format_comment(reason, summary, details, label=NEEDS_HUMAN if needs_human else BLOCKED)
    repeated = _repeats(ctx.store, n, body, {BLOCKED, NEEDS_HUMAN} if needs_human else {BLOCKED})
    run = ctx.store.get_run(n)
    if run is not None and run.status == "running":
        ctx.store.update_run(n, detail=f"{summary}\n{details}" if details else summary)
    ctx.store.save_flag(n, reason, flagged_at=ctx.now().astimezone(UTC).isoformat())
    ctx.tracker.add_labels(n, [BLOCKED, NEEDS_HUMAN] if needs_human else [BLOCKED])
    ctx.tracker.remove_labels(n, [READY, RUNNING])
    board.sync_status(ctx, n, "Blocked")
    if not repeated:  # The same blocker again is no new public fact.
        ctx.tracker.comment(n, body)


def _repeats(store: StateStore, n: int, body: str, labels: set[str]) -> bool:
    """Whether the last observed issue already carries these labels and this exact blocker."""
    row = next((row for row in store.local_issues() if row["number"] == n), None)
    if row is None or not labels <= set(row["labels"]):
        return False
    flags = [c for c in row.get("comments", []) if flag_marker(c.get("body", "")) is not None]
    latest = max(flags, key=lambda c: (datetime.fromisoformat(c["created_at"]), c["id"]), default=None)
    return latest is not None and latest["body"] == body


def invalidate_approval(
    ctx: FlagContext, n: int, summary: str, details: str | None = None,
) -> None:
    flag(ctx, n, Reason.HASH_MISMATCH, summary, details)
    ctx.tracker.remove_labels(n, [APPROVED])
    ctx.store.clear_approval(n)

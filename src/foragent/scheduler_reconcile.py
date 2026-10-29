"""Record the repair of a running label that has no run record (#130)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent import orch_events, scheduler
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext


def orphan_crash_detail(ctx: TickContext, number: int, run: Run | None) -> str:
    """Return the label generation's crash detail, journaling ``reconciled`` first when no run exists.

    The event is written before the stand-in crashed record: the journal is its only
    record, so a failed write leaves nothing that would make the next tick skip it.
    """
    detail = scheduler._crash_detail(ctx, number)
    if run is None and not orch_events.has_event(ctx.store, number, "reconciled", detail=detail):
        scheduler.append_event(ctx.store, ctx.now(), number, "reconciled",
                               scheduler.text("reconciled", ctx.config.language), detail=detail)
    return detail

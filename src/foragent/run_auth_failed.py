"""End a run whose Git or GitHub authentication kept failing (#1208).

The transport adapters already retried within their budget. The run ends as
``auth-failed`` with one detail line and one journal event. It is neither
blocked nor put back in the queue: the tick holds an issue whose last run is
``auth-failed`` until ``fa orch retry`` adds ``ready`` (or a new approval), so
automatic restarts cannot spend quota on a broken credential.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import TYPE_CHECKING

from foragent import auth_retry

if TYPE_CHECKING:
    from foragent.runner import RunContext

OUTCOME = auth_retry.OUTCOME


def is_auth_failure(exc: BaseException) -> bool:
    return bool(getattr(exc, "is_auth_failure", False))


def merged_in_this_run(ctx: RunContext, n: int) -> bool:
    """Whether this execution already merged its PR (record_merge saves ``merged_at`` beside the record)."""
    run = ctx.store.get_run(n)
    try:
        merged = json.loads((ctx.store.root / "runs" / f"{n}.json").read_text(encoding="utf-8")).get("merged_at")
        return bool(run and merged and datetime.fromisoformat(merged) >= datetime.fromisoformat(run.started_at))
    except (OSError, ValueError, TypeError):
        return False


def outcome(ctx: RunContext, n: int) -> str:
    """auth-failed, unless the merge already happened: then finalization failed and the run stays merged."""
    return "merged" if merged_in_this_run(ctx, n) else OUTCOME


def finish(n: int, ctx: RunContext, exc: BaseException, label: str | None) -> tuple[str, str]:
    """Record the one-line reason and drop ``label`` if given; return (outcome, detail)."""
    from foragent.orch_events import append_event
    from foragent.tracker import TrackerError

    result = outcome(ctx, n)
    detail = auth_retry.describe(exc) + (" (after the merge)" if result == "merged" else "")
    ctx.store.update_run(n, outcome=result, detail=detail)
    append_event(ctx.store, ctx.now(), n, OUTCOME, detail)
    if label is not None:
        try:
            ctx.tracker.remove_labels(n, [label])
        except TrackerError as error:
            # The same credential may refuse this too; the tick removes a stale label.
            append_event(ctx.store, ctx.now(), n, "cleanup-failed",
                         f"running label removal failed: {auth_retry.first_line(str(error))}")
    return result, detail

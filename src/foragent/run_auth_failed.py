"""End a run whose Git or GitHub authentication kept failing (#1208).

The transport adapters already retried within their budget. The run ends as
``auth-failed`` with one detail line and one journal event. It is neither
blocked nor put back in the queue: the tick holds an issue whose last run is
``auth-failed`` until a later ``fa orch retry`` (or a new approval), so
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


def _events_since(store, n: int, since: str) -> list[dict]:
    """This issue's journal events at or after ``since`` (the run's start)."""
    path = store.root / "orch-events.jsonl"
    try:
        start = datetime.fromisoformat(since)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError, TypeError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
            if event.get("issue") == n and datetime.fromisoformat(event["at"]) >= start:
                events.append(event)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return events


def recorded_detail(ctx: RunContext, n: int) -> str:
    """The reason ``finish`` recorded for this execution, derived from the journal, or ""."""
    run = ctx.store.get_run(n)
    if run is None:
        return ""
    reasons = [event["summary"] for event in _events_since(ctx.store, n, run.started_at) if event["kind"] == OUTCOME]
    return reasons[-1] if reasons else ""


def retry_requested_at(store, n: int) -> datetime | None:
    """When `fa orch retry` asked for this issue, from the marker it writes before adding `ready`."""
    from foragent.run_previous import retry_marker

    try:
        return datetime.fromisoformat(retry_marker(store.root, n).read_text(encoding="utf-8").split()[1])
    except (OSError, IndexError, ValueError):
        return None


def held(store, n: int, approved_at: str) -> bool:
    """An auth-failed execution waits for `fa orch retry` or a newer approval.

    A ``ready`` label proves nothing (the runner's own limit wait adds one too),
    and neither does a journal line written after the label. The retry counts
    only when it was requested after this execution started: the request that
    launched this execution is older than its start, so it cannot launch another.
    """
    run = store.get_run(n)
    if run is None or run.outcome != OUTCOME:
        return False
    try:
        started = datetime.fromisoformat(run.started_at)
        if datetime.fromisoformat(approved_at) > started:
            return False
        requested = retry_requested_at(store, n)
        return requested is None or not requested > started
    except (TypeError, ValueError):
        return True


def record_after(n: int, ctx: RunContext, failure: object) -> None:
    """Record a refused credential in a step after the merge (the reinstall's fetch); the run stays merged."""
    if is_auth_failure(failure):
        finish(n, ctx, failure, None)

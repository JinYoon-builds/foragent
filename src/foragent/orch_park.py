"""Park intent belongs to issue comments, never to the prunable machine journal.

Only these verbs write park/unpark markers. RecordingTracker owns their delivery
and cache; the gate reads that projection without posting transitions. Parking
revokes approval; neither unpark nor history pruning restores it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import re

from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, WAITING_PERSON
from foragent.state import StateStore


def _kind(body: str, n: int) -> str | None:
    first, _, summary = body.partition("\n")
    if match := re.fullmatch(r"<!-- foragent:orch kind=(parked|unparked) -->", first.rstrip("\r")):
        return match[1]
    if first.rstrip("\r") == "<!-- foragent:orch -->":
        # Previously published audit comments already carry the complete intent.
        if match := re.match(rf"#{n}: (parked|unparked)(?=;|\s|$)", summary):
            return match[1]
    return None


def _latest(store: StateStore, n: int, comments=None):
    if comments is None:
        row = next((row for row in store.local_issues() if row["number"] == n), {})
        comments = row.get("comments", [])
    # Undelivered intent follows the remote facts in FIFO order. Its synthetic
    # negative comment ids must not reverse two commands queued at the same time.
    pending = [entry for entry in store.github_outbox()
               if entry["issue"] == n and entry["action"] == "comment"
               and _kind(entry["body"], n) is not None]
    if pending:
        entry = pending[-1]
        return _kind(entry["body"], n), datetime.fromisoformat(entry["queued_at"])
    markers = [(datetime.fromisoformat(c["created_at"]), c["id"], kind)
               for c in comments if (kind := _kind(c["body"], n)) is not None]
    if not markers:
        return None
    at, _, kind = max(markers)
    return kind, at


def parked_at(store: StateStore, n: int, *, comments=None) -> datetime | None:
    """Time of the current parked intent, or None after an explicit unpark."""
    latest = _latest(store, n, comments)
    return latest[1] if latest is not None and latest[0] == "parked" else None


def is_parked(store: StateStore, n: int, *, comments=None) -> bool:
    """Read the newest issue marker, overlaid by explicit undelivered intent."""
    return parked_at(store, n, comments=comments) is not None


def _record(ctx, tracker, n: int, kind: str, summary: str) -> None:
    from foragent.orch_events import append_event

    summary = " ".join(summary.split())
    tracker.comment(n, f"<!-- foragent:orch kind={kind} -->\n{summary}")
    append_event(ctx.store, ctx.now(), n, kind, summary)


def park(ctx, n: int, why: str, *, for_person: bool = False) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    orch._issue(ctx, n)
    why = orch._text(why, "why")
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    if for_person:
        tracker.add_labels(n, [WAITING_PERSON])
    # Only a new approval (fa order publish) lets the gate start it again.
    tracker.remove_labels(n, [APPROVED, BLOCKED, NEEDS_HUMAN])
    summary = f"#{n}: parked" + (" for a person's decision" if for_person else "") + f"; why: {why}"
    _record(ctx, tracker, n, "parked", summary)
    return summary


def unpark(ctx, n: int) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    orch._issue(ctx, n)
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    # An explicit command reads GitHub now; gate readers use the existing cache.
    # During a GitHub pause the persisted projection and outbox are the evidence.
    comments = None if ctx.store.github_wait_until(ctx.now()) is not None else [
        asdict(comment) for comment in tracker.list_comments(n)]
    if not is_parked(ctx.store, n, comments=comments):
        raise orch.OrchError(f"issue #{n} is not parked")
    tracker.remove_labels(n, [WAITING_PERSON])
    summary = f"#{n}: unparked"
    _record(ctx, tracker, n, "unparked", summary)
    return summary

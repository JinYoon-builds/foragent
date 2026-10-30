"""`fa orch park|unpark`: the orchestrator's recorded answer to a blocked or held issue.

These verbs are the only writers of the waiting-person label and of the parked and
unparked journal events; the gate tick only reads them (``parked_at``). Park also
removes the approved label, so a parked issue never starts; unpark does not restore it.
"""

from __future__ import annotations

from datetime import datetime
import json

from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, WAITING_PERSON
from foragent.state import StateStore


def _issue_events(store: StateStore, n: int) -> list[dict]:
    path = store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("issue") == n and event.get("kind") in {"parked", "unparked"}:
            events.append(event)
    return events


def parked_at(store: StateStore, n: int) -> datetime | None:
    """The time of the issue's newest parked event, or None."""
    times = [datetime.fromisoformat(event["at"]) for event in _issue_events(store, n) if event["kind"] == "parked"]
    return max(times, default=None)


def is_parked(store: StateStore, n: int) -> bool:
    """True when the issue's last park/unpark event is a park."""
    events = _issue_events(store, n)
    return bool(events) and events[-1]["kind"] == "parked"


def park(ctx, n: int, why: str, *, for_person: bool = False) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    orch._issue(ctx, n)
    why = orch._text(why, "why")
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    if for_person:
        tracker.add_labels(n, [WAITING_PERSON])
    # Park revokes admission; only a new approval (fa order publish) lets the gate start it again.
    tracker.remove_labels(n, [APPROVED, BLOCKED, NEEDS_HUMAN])
    summary = f"#{n}: parked" + (" for a person's decision" if for_person else "") + f"; why: {why}"
    orch._event(ctx, n, "parked", summary)
    return summary


def unpark(ctx, n: int) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    orch._issue(ctx, n)
    if not is_parked(ctx.store, n):
        raise orch.OrchError(f"issue #{n} is not parked")
    RecordingTracker(ctx.tracker, ctx, queue_writes=True).remove_labels(n, [WAITING_PERSON])
    summary = f"#{n}: unparked"
    orch._event(ctx, n, "unparked", summary)
    return summary

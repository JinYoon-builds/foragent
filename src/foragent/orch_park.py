"""Park intent belongs to issue comments, never to the prunable machine journal.

Only these verbs write park/unpark markers. RecordingTracker owns their delivery
and cache; the gate reads that projection without posting transitions. Parking
revokes approval; neither unpark nor history pruning restores it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import re

from foragent import order_waiting
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, WAITING_PERSON
from foragent.orch_transition import transition_state
from foragent.reasons import comment_order, one_line
from foragent.state import StateStore


def _kind(body: str, n: int) -> str | None:
    marker = re.match(r"<!-- foragent:orch(?: kind=(parked|unparked))? -->(?=\s|$)", body)
    if marker is not None:
        if marker[1] is not None:
            return marker[1]
        # Previously published one-line and multiline audits carry the same intent.
        if match := re.match(rf"#{n}: (parked|unparked)(?=;|\s|$)", body[marker.end():].lstrip()):
            return match[1]
    return None


def _latest(store: StateStore, n: int, comments=None):
    if comments is None:
        row = next((row for row in store.local_issues() if row["number"] == n), {})
        comments = row.get("comments", [])
    # Pending comments remain explicit intent, outside the issue projection.
    pending = [entry for entry in store.github_outbox()
               if entry["issue"] == n and entry["action"] == "comment"
               and _kind(entry["body"], n) is not None]
    if pending:
        entry = pending[-1]
        return _kind(entry["body"], n), datetime.fromisoformat(entry["queued_at"])
    markers = [(comment_order(c), kind)
               for c in comments if (kind := _kind(c["body"], n)) is not None]
    if not markers:
        return None
    order, kind = max(markers)
    return kind, order[0]


def parked_at(store: StateStore, n: int, *, comments=None) -> datetime | None:
    """Time of the current parked intent, or None after an explicit unpark."""
    latest = _latest(store, n, comments)
    return latest[1] if latest is not None and latest[0] == "parked" else None


def is_parked(store: StateStore, n: int, *, comments=None) -> bool:
    """Read the newest issue marker, overlaid by explicit undelivered intent."""
    return parked_at(store, n, comments=comments) is not None


def _record(ctx, tracker, n: int, kind: str, summary: str, waiting: str, *, changed: bool) -> None:
    from foragent.orch_events import append_event

    if changed:
        tracker.comment(n, one_line(f"<!-- foragent:orch kind={kind} -->",
                                   summary + (f" {waiting}" if waiting else "")))
    append_event(ctx.store, ctx.now(), n, kind, summary)


def _waiting(ctx, tracker, n: int, why: str | None, for_person: bool = False) -> tuple[str, bool]:
    # Pending writes are explicit intent: do not replace their body with an older remote read.
    issue = tracker.current_issue(n)
    role = "person" if for_person else "orchestrator"
    condition = ("Record the person's decision, then explicitly unpark this issue." if for_person else
                 "The orchestrator confirms the reason is resolved, then explicitly unparks this issue.")
    text = (f"Reason: {' '.join(why.split())}\nNext condition: {condition}\nResponsible role: {role}"
            if why is not None else None)
    body = order_waiting.replace_section(issue.body, text)
    body = order_waiting.mark_write(issue.body, order_waiting.stamp(issue.body, body))
    if body != issue.body:
        tracker.update_issue(n, title=issue.title, body=body)
    # The park marker also announces the wait change; publication must not announce it twice.
    return (order_waiting.notification(body, ctx.config.language),
            order_waiting.section(issue.body) != order_waiting.section(body))


def park(ctx, n: int, why: str, *, for_person: bool = False) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    before = transition_state(ctx, n, orch._issue(ctx, n))
    why = orch._text(why, "why")
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    was_parked = is_parked(ctx.store, n)
    if for_person:
        tracker.add_labels(n, [WAITING_PERSON])
    # Only a new approval (fa order publish) lets the gate start it again.
    tracker.remove_labels(n, [APPROVED, BLOCKED, NEEDS_HUMAN])
    summary = f"#{n}: parked" + (" for a person's decision" if for_person else "") + f"; why: {why}"
    after = before.after(add=[WAITING_PERSON] if for_person else [], remove=[APPROVED, BLOCKED, NEEDS_HUMAN])
    waiting, wait_changed = _waiting(ctx, tracker, n, why, for_person)
    _record(ctx, tracker, n, "parked", summary, waiting,
            changed=before != after or wait_changed or not was_parked)
    return summary


def unpark(ctx, n: int) -> str:
    from foragent import orch
    from foragent.tracker import RecordingTracker

    before = transition_state(ctx, n, orch._issue(ctx, n))
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    # An explicit command reads GitHub now; gate readers use the existing cache.
    # During a GitHub pause the persisted projection and outbox are the evidence.
    comments = None if ctx.store.github_wait_until(ctx.now()) is not None else [
        asdict(comment) for comment in tracker.list_comments(n)]
    was_parked = is_parked(ctx.store, n, comments=comments)
    if not was_parked:
        raise orch.OrchError(f"issue #{n} is not parked")
    tracker.remove_labels(n, [WAITING_PERSON])
    summary = f"#{n}: unparked"
    waiting, wait_changed = _waiting(ctx, tracker, n, None)
    # A validated parked intent becomes unparked even when it has no waiting label.
    _record(ctx, tracker, n, "unparked", summary, waiting,
            changed=before != before.after(remove=[WAITING_PERSON]) or wait_changed or was_parked)
    return summary

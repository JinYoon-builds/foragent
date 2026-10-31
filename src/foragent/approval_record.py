"""Locked blob hashes a re-approval records on its issue (#1107).

Publication writes one marker comment right before it adds ``approved``; every
gate, on any clone, reads it from the tracker. The record belongs to the
approval it precedes: the newest marker comment no later than that approval and
no earlier than the issue's previous ``approved`` label event. A later approval
made by hand therefore never inherits an old record.
"""

from __future__ import annotations

from datetime import datetime
import json

from foragent.tracker import Comment, LabelEvent

MARKER = "<!-- foragent:approval-blobs "


def comment_body(blobs: dict[str, str]) -> str:
    return f"{MARKER}{json.dumps(dict(sorted(blobs.items())))} -->\n"


def _parse(body: str) -> dict[str, str] | None:
    start = body.find(MARKER)
    if start < 0:
        return None
    end = body.find(" -->", start)
    try:
        value = json.loads(body[start + len(MARKER):end])
    except ValueError:
        return None
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return None
    return value


def _earlier(events: list[LabelEvent], event: LabelEvent) -> list[datetime]:
    position = max((i for i, item in enumerate(events) if item == event), default=len(events))
    return [datetime.fromisoformat(item.created_at) for item in events[:position] if item.label == event.label]


def possible(events: list[LabelEvent], event: LabelEvent) -> bool:
    """Whether ``event`` can have a record: publication records only re-approvals."""
    return bool(_earlier(events, event))


def recorded(events: list[LabelEvent], comments: list[Comment], event: LabelEvent) -> dict[str, str]:
    """The blob hashes recorded for the approval ``event``, or ``{}`` when there are none."""
    at = datetime.fromisoformat(event.created_at)
    floor = max(_earlier(events, event), default=None)
    found: tuple[datetime, int, dict[str, str]] | None = None
    for comment in comments:
        blobs = _parse(comment.body)
        when = datetime.fromisoformat(comment.created_at)
        if blobs is None or when > at or (floor is not None and when < floor):
            continue
        if found is None or (when, comment.id) >= found[:2]:
            found = (when, comment.id, blobs)
    return found[2] if found else {}

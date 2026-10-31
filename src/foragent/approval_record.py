"""Locked blob hashes a re-approval records on its issue (#1107).

Publication writes one marker comment right before it adds ``approved``; every
gate, on any clone, reads it from the tracker. A record names the approval it is
for by position: the n-th ``approved`` label added to the issue. Only a comment
by that approval's own actor counts. Positions do not depend on timestamps, so a
later approval made by hand, even in the same second, never inherits an old
record, and nobody else can write one for an approver.
"""

from __future__ import annotations

import json

from foragent.tracker import Comment, LabelEvent

MARKER = "<!-- foragent:approval-blobs "


def comment_body(blobs: dict[str, str], approval: int) -> str:
    """The marker for the ``approval``-th approval (see ``next_approval``)."""
    return f"{MARKER}{json.dumps({'approval': approval, 'blobs': dict(sorted(blobs.items()))})} -->\n"


def _added(events: list[LabelEvent], label: str) -> int:
    return sum(1 for item in events if item.label == label and item.action == "labeled")


def next_approval(events: list[LabelEvent]) -> int:
    """The position the next added ``approved`` label will have."""
    return _added(events, "approved") + 1


def _parse(body: str) -> tuple[int, dict[str, str]] | None:
    start = body.find(MARKER)
    if start < 0:
        return None
    end = body.find(" -->", start)
    try:
        value = json.loads(body[start + len(MARKER):end])
    except ValueError:
        return None
    if not isinstance(value, dict) or type(value.get("approval")) is not int:
        return None
    blobs = value.get("blobs")
    if not isinstance(blobs, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in blobs.items()):
        return None
    return value["approval"], blobs


def possible(events: list[LabelEvent], event: LabelEvent) -> bool:
    """Whether ``event`` can have a record: publication records only re-approvals."""
    position = max((i for i, item in enumerate(events) if item == event), default=None)
    return position is not None and any(item.label == event.label for item in events[:position])


def recorded(events: list[LabelEvent], comments: list[Comment], event: LabelEvent) -> dict[str, str]:
    """The blob hashes recorded for the approval ``event``, or ``{}`` when there are none."""
    position = max((i for i, item in enumerate(events) if item == event), default=None)
    if position is None:
        return {}
    approval = _added(events[:position + 1], event.label)
    found: tuple[int, dict[str, str]] | None = None
    for comment in comments:
        parsed = _parse(comment.body)
        if parsed is None or parsed[0] != approval or comment.author.casefold() != event.actor.casefold():
            continue
        if found is None or comment.id > found[0]:
            found = (comment.id, parsed[1])
    return found[1] if found else {}

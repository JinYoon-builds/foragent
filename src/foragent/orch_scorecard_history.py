"""Issue history for scorecard replay, from the retained ``issue-state`` flow records.

Each record carries an issue's sorted labels, its native blocked_by edges (None when not
read) and its state at the record's time. Replay uses these facts after the issue closes or
leaves the listing, so today's rows never rewrite earlier minutes. Time before an issue's
first retained record has no history here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from foragent.labels import issue_lane
from foragent.orch_facts_resources import _time
from foragent.state import StateStore

Span = tuple[datetime | None, datetime | None]


def records(store: StateStore) -> dict[int, list[tuple[datetime, dict[str, Any]]]]:
    """Each issue's retained issue-state facts, oldest first."""
    found: dict[int, list[tuple[datetime, dict[str, Any]]]] = {}
    for record in store.flow_records():
        if record["kind"] == "issue-state" and type(record["issue"]) is int and (at := _time(record["at"])):
            found.setdefault(record["issue"], []).append((at, record["data"]))
    return found


def blocked_spans(history: list[tuple[datetime, dict[str, Any]]]) -> list[Span]:
    """Each recorded open blocker, from its record until a later record read the edges again."""
    known = [(at, data["blocked_by"]) for at, data in history if isinstance(data.get("blocked_by"), list)]
    spans: list[Span] = []
    for index, (at, edges) in enumerate(known):
        if any(isinstance(edge, dict) and edge.get("state") == "open" for edge in edges):
            spans.append((at, known[index + 1][0] if index + 1 < len(known) else None))
    return spans


def first_edges(history: list[tuple[datetime, dict[str, Any]]]) -> datetime | None:
    """When the edges were first recorded; earlier time has no recorded dependency history."""
    return next((at for at, data in history if isinstance(data.get("blocked_by"), list)), None)


def view(store: StateStore, number: int, history: list[tuple[datetime, dict[str, Any]]],
         lanes: dict) -> dict[str, Any]:
    """An issue with no current row, replayed only from its retained records."""
    events, labels = [], set()
    for at, data in history:
        now = set(data.get("labels") or ())
        events += [(at, label, "labeled") for label in sorted(now - labels)]
        events += [(at, label, "unlabeled") for label in sorted(labels - now)]
        labels = now
    left = None  # When it last stopped being open, if it did.
    for at, data in history:
        if data.get("state") == "open":
            left = None
        elif left is None:
            left = at
    approval = store.load_approval(number)
    return {"events": events, "approved_at": _time(approval.approved_at) if approval else None,
            "labels": labels, "open_until": left, "closed": left is not None,
            "blocked_until": blocked_spans(history), "flagged_at": None, "updated_at": None,
            "lane": issue_lane(sorted(labels), lanes)}

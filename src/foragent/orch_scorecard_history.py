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


def blocked_spans(history: list[tuple[datetime, dict[str, Any]]], *, either: bool = False) -> list[Span]:
    """Each recorded open blocker, from its record until a later record read the edges again.

    ``either`` (demand) also counts the gap before a record that saw an open blocker, because
    the blocker appeared at an unrecorded time inside it."""
    known = [(at, data["blocked_by"]) for at, data in history if isinstance(data.get("blocked_by"), list)]
    blocked = [any(isinstance(edge, dict) and edge.get("state") == "open" for edge in edges) for _, edges in known]
    spans: list[Span] = []
    for index, (at, _) in enumerate(known):
        later = index + 1 < len(known)
        if blocked[index] or (either and later and blocked[index + 1]):
            spans.append((at, known[index + 1][0] if later else None))
    return spans


def first_edges(history: list[tuple[datetime, dict[str, Any]]]) -> datetime | None:
    """When the edges were first recorded; earlier time has no recorded dependency history."""
    return next((at for at, data in history if isinstance(data.get("blocked_by"), list)), None)


READY = frozenset({"approved", "ready"})
HOLDING = frozenset({"blocked", "needs-human", "running"})  # Labels that keep an approved issue from starting.


def states(history: list[tuple[datetime, dict[str, Any]]]) -> list[tuple[datetime, frozenset, str | None]]:
    """(time, labels, state) per record: what each observation saw, never when a label was added."""
    return [(at, frozenset(data.get("labels") or ()), data.get("state")) for at, data in history]


def _around(issue: dict[str, Any], moment: datetime) -> list[tuple[datetime, frozenset, str | None]] | None:
    """The observations bounding ``moment``: the last one at or before it and the next one.

    A change between two observations happened at an unrecorded time, so both can be true
    there. None before the first observation or without history."""
    seen = issue.get("history") or ()
    index = next((i for i in range(len(seen) - 1, -1, -1) if seen[i][0] <= moment), None)
    if index is None:
        return None
    return list(seen[index:index + 2])


def labels_at(issue: dict[str, Any], moment: datetime) -> set[str] | None:
    """Labels certain enough for demand at ``moment``, from observations when no label event exists.

    Approval and ready count only when every bounding observation had them; a hold counts when
    any had it. None defers to the row's own rule; an issue known only from history has no
    labels before its first observation (unknown, not ready)."""
    if issue["events"]:
        return None
    around = _around(issue, moment)
    if around is None:
        return set() if issue.get("history_only") else None
    every = frozenset.intersection(*(labels for _, labels, _ in around))
    return set(every) | {label for _, labels, _ in around for label in labels & HOLDING}


def closed_at(issue: dict[str, Any], moment: datetime) -> bool:
    """Not certainly open at ``moment``: any bounding observation not open; before history, the row's rule.

    A confirmed merge dates only the close it caused: one inside the gap from an open observation
    to the next, not-open observation. An earlier close, reopened later, keeps its unknown time."""
    around = _around(issue, moment)
    if around is not None:
        if len(around) == 2 and around[0][2] == "open" and any(
                around[0][0] <= merged <= around[1][0] and moment < merged for merged in issue.get("merges", ())):
            return False
        return any(state != "open" for _, _, state in around)
    if issue.get("history_only"):
        return True
    return issue["closed"] and (issue["open_until"] is None or moment >= issue["open_until"])


def _event_lane(issue: dict[str, Any], moment: datetime) -> str | None:
    """The lane exact label events give at ``moment``; None when the events never name a lane."""
    if not any(str(label).startswith("lane:") for _, label, _ in issue["events"]):
        return None
    labels: set[str] = set()
    for at, label, action in issue["events"]:
        if at > moment:
            break
        (labels.add if action == "labeled" else labels.discard)(label)
    return issue_lane(sorted(labels), issue["lanes"])


def lanes_at(issue: dict[str, Any], moment: datetime) -> set[str]:
    """Every lane a bounding observation placed it in; exact label events decide when they name
    a lane; before history, today's lane."""
    if (exact := _event_lane(issue, moment)) is not None:
        return {exact}
    around = _around(issue, moment)
    if around is None:
        return {issue["lane"]}
    return {issue_lane(sorted(labels), issue["lanes"]) for _, labels, _ in around}


def lane_at(issue: dict[str, Any], moment: datetime) -> str:
    """The lane exact events or the last observation at or before ``moment`` give; else today's lane."""
    if (exact := _event_lane(issue, moment)) is not None:
        return exact
    around = _around(issue, moment)
    return issue["lane"] if around is None else issue_lane(sorted(around[0][1]), issue["lanes"])


def view(store: StateStore, number: int, history: list[tuple[datetime, dict[str, Any]]],
         lanes: dict, merges: list[datetime] = ()) -> dict[str, Any]:
    """An issue with no current row, replayed only from its retained records.

    Observations say which labels were present, not when they were added, so they create no
    label events: its approval time comes only from the approval snapshot."""
    seen = states(history)
    labels, state = (set(seen[-1][1]), seen[-1][2]) if seen else (set(), None)
    approval = store.load_approval(number)
    return {"events": [], "history": seen, "history_only": True, "lanes": lanes,
            "approved_at": _time(approval.approved_at) if approval else None,
            "labels": labels, "open_until": max(merges, default=None), "merges": list(merges),
            "closed": state != "open",
            "blocked_until": blocked_spans(history, either=True), "flagged_at": None, "updated_at": None,
            "lane": issue_lane(sorted(labels), lanes)}

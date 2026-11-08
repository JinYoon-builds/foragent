"""Standalone latency for `fa orch scorecard`: size s issues approved into an otherwise empty queue.

A standalone sample is a merged size s issue whose latest approval before its merge came
while no other executable issue of this repository was queued (approved, ready, not held)
or running. It measures approval to the recorded merge and sits in the bucket of its merge.

Queue state at an approval comes only from append-only local records: issue observations
(``issue-state`` flow records), label events and run records. Facts a later change overwrites
(the approval snapshot, today's dependency edges) never decide a past moment. Before the oldest
retained observation the queue is unknown, so such an approval is counted as missing evidence,
never as an empty queue. Between two observations a change happened at an unrecorded time, so each other
issue is judged in every state it may have had then: busy in all, not busy in all, or unknown.
Any unknown other issue makes the sample missing evidence. (Idle's certain-demand readers lean
the other way, toward "not queued", so they are not used here.)
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from foragent import orch_scorecard_flow as flow, orch_scorecard_history as history
from foragent.orch_scorecard_records import _executions, _issues, approval_before
from foragent.state import StateStore

SIZE = "size:s"


def _evidence_since(past: dict[int, list[tuple[datetime, dict[str, Any]]]]) -> datetime | None:
    """The oldest retained issue observation; None when none is retained.

    Only observations show which issues waited in the queue. A run record shows only its own
    run, so it never starts the period in which the queue can be told."""
    return min((at for facts in past.values() for at, _ in facts), default=None)


def _small(issue: dict[str, Any], moment: datetime) -> bool:
    """Size s at ``moment``: label events where recorded, else the issue's labels as observed."""
    if issue["events"]:
        labels: set[str] = set()
        for at, label, action in issue["events"]:
            if at > moment:
                break
            (labels.add if action == "labeled" else labels.discard)(label)
        if labels:
            return SIZE in labels
    observed = [labels for at, labels, _ in issue["history"] if at <= moment]
    return SIZE in (observed[-1] if observed else issue["labels"])


HELD = frozenset({"blocked", "needs-human"})


def _agree(values: list[bool | None]) -> bool | None:
    """One answer when every bounding fact gives it; None when they differ or one is unknown."""
    return values[0] if values and all(value is values[0] for value in values) else None


def _and(*values: bool | None) -> bool | None:
    """Three-valued AND: one certain False decides; otherwise any unknown stays unknown."""
    return False if any(value is False for value in values) else None if None in values else True


def _or(*values: bool | None) -> bool | None:
    return True if any(value is True for value in values) else None if None in values else False


def _bounding(facts: list[tuple[datetime, Any]], moment: datetime) -> list[tuple[datetime, Any]] | None:
    """The fact at or before ``moment`` and the next one; None before the first fact."""
    index = next((i for i in range(len(facts) - 1, -1, -1) if facts[i][0] <= moment), None)
    return None if index is None else facts[index:index + 2]


def _row_open(issue: dict[str, Any], moment: datetime) -> bool | None:
    """Open at ``moment`` from the row's own times, for time before its first observation."""
    if issue.get("history_only"):
        return None
    if not issue["closed"]:
        return True
    return None if issue["open_until"] is None else moment < issue["open_until"]


def _states(issue: dict[str, Any], moment: datetime) -> list[tuple[set[str], bool | None]] | None:
    """Every (labels, open) state that may hold at ``moment``; None when the records cannot say.

    Exact label events give one label set; otherwise each bounding observation is one possible
    state, because the change between two observations happened at an unrecorded time. A
    recorded merge inside the gap from an open observation to a closed one dates the close."""
    seen = issue["history"]
    around = _bounding([(at, (labels, state)) for at, labels, state in seen], moment)
    if around is not None and len(around) == 2 and around[0][1][1] == "open" != around[1][1][1]:
        merges = [at for at in issue.get("merges", ()) if around[0][0] <= at <= around[1][0]]
        if merges and moment >= min(merges):
            return [(set(), False)]
        if merges:
            around = around[:1]  # Still open until the merge: the closed observation does not apply.
    if issue["events"]:
        labels: set[str] = set()
        for at, label, action in issue["events"]:
            if at > moment:
                break
            (labels.add if action == "labeled" else labels.discard)(label)
        opened = _row_open(issue, moment) if around is None else _agree([state == "open" for _, (_, state) in around])
        return [(labels, opened)]
    if around is None:
        return None
    return [(set(labels), state == "open") for _, (labels, state) in around]


def _blocked(issue: dict[str, Any], edges: list[tuple[datetime, Any]], moment: datetime) -> bool | None:
    """An open native blocker at ``moment`` from the bounding edge reads; before any read, today's edges.

    Today's open edge may have been added after ``moment`` and today's closed edge may have been
    open then, so without a read at or before ``moment`` any current edge leaves it unknown."""
    known = [(at, any(isinstance(edge, dict) and edge.get("state") == "open" for edge in data))
             for at, data in edges if isinstance(data, list)]
    around = _bounding(known, moment)
    if around is not None:
        return _agree([value for _, value in around])
    return None  # No dependency read at or before ``moment``: today's edges cannot date a past edge.


def _busy(issue: dict[str, Any], edges: list[tuple[datetime, Any]], moment: datetime) -> bool | None:
    """Queued (approved, ready, not held, not blocked) or running at ``moment``; None when the records
    cannot tell. Each possible state is judged whole, then all must agree: unlike idle's
    certain-demand rule, uncertainty never reads as an empty queue."""
    states = _states(issue, moment)
    if states is None:
        # No observation and no label event at or before ``moment``. Observations are retained
        # completely from the oldest one on, so the issue was not seen anywhere then. The approval
        # snapshot is not used: a later re-approval or invalidation overwrites it.
        return False
    blocked = _blocked(issue, edges, moment)
    return _agree([_and(opened, _or("running" in labels, _and(
        {"approved", "ready"} <= labels and not labels & HELD, _not(blocked)))) for labels, opened in states])


def _not(value: bool | None) -> bool | None:
    return None if value is None else not value


def _queue_at(number: int, moment: datetime, issues: dict[int, dict[str, Any]], executions: list[dict[str, Any]],
              past: dict[int, list[tuple[datetime, dict[str, Any]]]]) -> bool | None:
    """True when another issue was certainly queued or running at ``moment``, False when every other
    issue was certainly not, None when any could not be told from the retained records."""
    for row in executions:
        if row["number"] != number and row["started"] <= moment and (row["finished"] is None or moment < row["finished"]):
            return True
    answers = []
    for other, issue in issues.items():
        if other == number:
            continue
        edges = [(at, data.get("blocked_by")) for at, data in past.get(other, ())]
        answers.append(_busy(issue, edges, moment))
    return _or(*answers)


def _bucket(moment: datetime, now: datetime, days: int) -> int | None:
    if moment > now:
        return None
    index = (now - moment) // flow.DAY
    return index if index < days else None


def _summary(minutes: list[float], missing: int) -> dict[str, Any]:
    return {"samples": len(minutes), "median_minutes": median(minutes) if minutes else None,
            "missing_evidence": missing}


def windows(store: StateStore, now: datetime, days: int, lanes: dict | None = None) -> tuple[dict, dict]:
    """(today, previous): samples, median minutes and missing-evidence count.

    The previous window pools every earlier bucket's samples, so its median is over issues,
    not a median of daily medians."""
    issues, executions = _issues(store, lanes or {}), _executions(store, now)
    past = history.records(store)
    since = _evidence_since(past)
    minutes: list[list[float]] = [[] for _ in range(days)]
    missing = [0] * days
    merges: dict[int, datetime] = {}
    for row in executions:  # Each issue's latest merge; a merged issue counts once.
        if row["merged"] and row["merged_at"] is not None:
            merges[row["number"]] = max(row["merged_at"], merges.get(row["number"], row["merged_at"]))
    for number, merged in merges.items():
        issue = issues.get(number)
        if (bucket := _bucket(merged, now, days)) is None or issue is None:
            continue  # Outside every window, or no retained row to tell its size.
        approved = approval_before(store, issues, number, merged)
        if approved is None:
            missing[bucket] += _small(issue, merged)  # Size s, but its approval time is not retained.
            continue
        if not _small(issue, approved):
            continue
        queue = None if since is None or approved < since else _queue_at(number, approved, issues, executions, past)
        if queue is None:
            missing[bucket] += 1  # Retained records cannot tell whether the queue was empty then.
        elif queue is False:
            minutes[bucket].append((merged - approved).total_seconds() / 60)
    today = _summary(minutes[0], missing[0])
    previous = _summary([value for day in minutes[1:] for value in day], sum(missing[1:]))
    return today, previous

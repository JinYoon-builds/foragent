"""Necessary waiting and lane-slot utilization for the four-part approval-to-merge split.

orch_scorecard_four counts every moment no work covers as wasted waiting. This module
decides which of those moments are necessary: the issue waited on a file (or, with no
Files, its lane) that a named peer execution held, and that peer was running then.
Running means the peer's execution was open (started, not yet finished); its phase does
not matter. A wait for a slot, a test slot or the quota stays wasted.

Evidence, all written by the gate already:

* ``overlap`` flow records with the execution's ``run_started_at``: the runner's in-run
  overlap holds (run_overlap), active until the next record of that execution or its end;
* ``overlap`` records with ``run_started_at = None``: the scheduler's admission holds
  before a run starts (overlap_files.can_start), active until the next such record or the
  issue's next execution start, and only while the latest ``ready`` record newer than the
  hold still says the issue waits on ``file-overlap``.

Each hold names its peers as ``{"issue", "run_started_at"}``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, NamedTuple

from foragent.orch_facts_resources import _time
from foragent.orch_scorecard_records import _value_at

DAY = timedelta(days=1)
ADMISSION_REASON = "file-overlap"  # scheduler_start's ready reason for a file or lane hold.


class Hold(NamedTuple):
    start: datetime
    end: datetime
    peers: frozenset[tuple[int, datetime]]
    admission: bool  # Recorded before a run started; a later ready record can end it.
    recorded: datetime


def _peers(data: dict[str, Any]) -> frozenset[tuple[int, datetime]]:
    return frozenset((peer["issue"], started) for hold in data.get("holds") or [] if isinstance(hold, dict)
                     for peer in hold.get("peers") or [] if isinstance(peer, dict)
                     and isinstance(peer.get("issue"), int) and (started := _time(peer.get("run_started_at"))))


def holds(records: list[dict[str, Any]], number: int, executions: list[dict[str, Any]],
          now: datetime) -> list[Hold]:
    """The issue's overlap holds as intervals, each with the peer executions it waited on."""
    starts = sorted(row["started"] for row in executions if row["number"] == number)
    ends = {row["started"]: row["finished"] or now for row in executions if row["number"] == number}
    chains: dict[str | None, list[tuple[datetime, dict]]] = {}
    for row in records:
        if row["kind"] == "overlap" and row["issue"] == number and (at := _time(row["at"])) is not None:
            chains.setdefault(row["run_started_at"], []).append((at, row["data"]))
    out = []
    for run, chain in chains.items():
        chain.sort(key=lambda item: item[0])
        if run is None:
            limit = None
        elif (started := _time(run)) is None or started not in ends:
            continue
        else:
            limit = ends[started]
        for index, (at, data) in enumerate(chain):
            peers = _peers(data)
            if not peers:
                continue
            stops = [chain[index + 1][0]] if index + 1 < len(chain) else []
            if run is None:
                # An admission hold ends when the issue's next execution starts.
                stops += [start for start in starts if start > at][:1] or [now]
            else:
                stops.append(limit)
            end = min(stops)
            if at < end:
                out.append(Hold(at, end, peers, run is None, at))
    return out


def _admission_cause(readies: list[tuple[datetime, dict]], hold: Hold, moment: datetime) -> bool:
    """Whether the hold still explains the wait: no newer ready record names another cause."""
    latest = None
    for at, data in readies:
        if at > moment:
            break
        latest = (at, data)
    if latest is None or latest[0] < hold.recorded:
        return True  # The hold is the newer evidence.
    return latest[1].get("outcome") == "waiting" and latest[1].get("reason") == ADMISSION_REASON


def evidence(records: list[dict[str, Any]], number: int, executions: list[dict[str, Any]], now: datetime):
    """The issue's holds, the ready records that gate admission holds, and the open executions."""
    readies = sorted(((at, row["data"]) for row in records
                      if row["kind"] == "ready" and row["issue"] == number and (at := _time(row["at"])) is not None),
                     key=lambda item: item[0])
    running = {(row["number"], row["started"]): row["finished"] or now for row in executions}
    return holds(records, number, executions, now), readies, running


def points(found) -> set[datetime]:
    """Every moment the evidence can change: hold edges, ready records, peer start and end."""
    spans, readies, running = found
    out = {at for hold in spans for at in (hold.start, hold.end)}
    out.update(at for at, _ in readies)
    for hold in spans:
        for peer in hold.peers:
            if peer in running:
                out.update((peer[1], running[peer]))
    return out


def necessary(found, low: datetime, high: datetime) -> bool:
    """Whether the slice [low, high] waited on a hold whose named peer execution was running."""
    spans, readies, running = found
    for hold in spans:
        if not (hold.start <= low and high <= hold.end):
            continue
        if hold.admission and not _admission_cause(readies, hold, low):
            continue
        if any(peer[1] <= low and high <= running[peer] for peer in hold.peers if peer in running):
            return True
    return False


def utilization(executions: list[dict[str, Any]], budgets: list[tuple[datetime, int]],
                now: datetime, days: int) -> list[float | None]:
    """Per 24-hour bucket: busy lane-slot minutes / available lane-slot minutes.

    Available is the recorded ``parallel`` budget times time, only while a budget is
    recorded. Busy is the open executions at each moment, capped by that budget. A bucket
    with no recorded budget has no utilization (None)."""
    out: list[float | None] = []
    for day in range(days):
        low, high = now - (day + 1) * DAY, now - day * DAY
        cuts = {low, high}
        cuts.update(at for row in executions for at in (row["started"], row["finished"] or now) if low < at < high)
        cuts.update(at for at, _ in budgets if low < at < high)
        busy = available = 0.0
        ordered = sorted(cuts)
        for a, b in zip(ordered, ordered[1:]):
            budget = _value_at(budgets, a)
            if not budget:
                continue
            running = sum(1 for row in executions if row["started"] <= a and b <= (row["finished"] or now))
            minutes = (b - a).total_seconds() / 60
            busy += min(running, budget) * minutes
            available += budget * minutes
        out.append(busy / available if available else None)
    return out

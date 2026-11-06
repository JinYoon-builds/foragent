"""Four-part approval-to-merge accounting and execution lane-slot utilization.

Phase spans name work rounds. Activity records supply CI provenance and fill gaps
in phase history. Every fact boundary cuts the interval, so every minute belongs
to exactly one part, including unrecorded time (wasted waiting).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

from foragent import orch_events
from foragent.orch_facts_resources import _time
from foragent.orch_scorecard_records import Interval, _budgets, _value_at, approval_before
from foragent.state import StateStore

DAY = timedelta(days=1)
PARTS = ("value_work", "wasted_work", "necessary_waiting", "wasted_waiting")
WORK_PHASES = {"implementing", "reviewing", "checking", "ci", "resolving-conflict"}


def mid(values):
    known = [value for value in values if value is not None]
    return median(known) if known else None


def _activities(records: list[dict[str, Any]]) -> list[tuple[str, Interval, dict]]:
    return [(data.get("activity"), (start, stop), data) for row in records
            if row["kind"] == "activity" and (data := row["data"])
            and (start := _time(data.get("started_at"))) is not None
            and (stop := _time(data.get("finished_at"))) is not None and start < stop]


def _continues(name, previous, start, phases, failures):
    if previous is None:
        return False
    end = previous[0][1]
    # Keep zero-duration work events: they still separate two work rounds.
    return not (any(other != name and other in WORK_PHASES and end <= a and b <= start
                    for other, (a, b) in phases)
                or any(end <= at <= start for at in failures))


def _spans(phases, activities, approved, merged, failures=(), first_pass=None):
    """Work spans in time order; activities only fill time with no recorded phase."""
    spans = [(name, (max(a, approved), min(b, merged))) for name, (a, b) in phases
             if max(a, approved) < min(b, merged)]
    fallback = []
    for name, (a, b), _ in activities:
        pieces = [(max(a, approved), min(b, merged))]
        for _, (low, high) in spans:
            pieces = [part for start, stop in pieces for part in
                      ((start, min(stop, low)), (max(start, high), stop)) if part[0] < part[1]]
        fallback.extend((name, piece) for piece in pieces if piece[0] < piece[1])
    previous, heads, result = {}, {}, []
    separators = [*phases, *((name, span) for name, span, _ in activities)]
    for name, span in sorted(set(spans + fallback), key=lambda item: item[1][0]):
        prior = previous.get(name)
        continues = _continues(name, prior, span[0], separators, failures)
        part = "wasted_waiting"
        if name in {"implementing", "reviewing", "checking"}:
            if continues:
                part = prior[1]
            else:
                # A check after a review PASS or after CI ran re-checks what was already checked.
                first = prior is None and (name != "checking" or (
                    (first_pass is None or span[0] < first_pass) and "ci" not in previous))
                part = "value_work" if first else "wasted_work"
        if name == "ci":
            ci = [data for activity, (a, b), data in activities
                  if activity == "ci" and a < span[1] and span[0] < b]
            known = {data["head"] for data in ci if data.get("head") is not None}
            causes = {data.get("cause") for data in ci}
            if known:
                if known - heads.keys():
                    part = "value_work"
                elif causes & {"base-changed", "group-failed"}:
                    part = "wasted_work"
                else:
                    part = heads[next(iter(known))]
                for head in known:
                    heads.setdefault(head, part)
            elif prior is None:
                part = "value_work"
            elif causes == {"initial"} and continues:
                part = prior[1]
            else:
                part = "wasted_work"
        elif name == "resolving-conflict":
            part = "wasted_work"
        elif name == "merging":
            part = "necessary_waiting"
        previous[name] = (span, part)
        result.append((name, span, part))
    return result


def _latest(records, kind, low):
    rows = [row for row in records if row["kind"] == kind
             and (at := _time(row["at"])) is not None and at <= low]
    return max(enumerate(rows), key=lambda item: (_time(item[1]["at"]), item[0]))[1] if rows else None


def _peer_running(holds, executions, low, high):
    peers = [peer for hold in holds for peer in hold.get("peers", [])]
    return any(row["number"] == peer.get("issue") and row["started"] == _time(peer.get("run_started_at"))
               and row["started"] <= low and (row["finished"] is None or row["finished"] >= high)
               for peer in peers for row in executions)


def _overlap_holds(records, executions, low):
    overlap = _latest(records, "overlap", low)
    if overlap is None:
        return []
    ready = _latest(records, "ready", low)
    if overlap.get("run_started_at") is None and ready and _time(ready["at"]) >= _time(overlap["at"]):
        data = ready["data"]
        if data.get("outcome") != "waiting" or data.get("reason") != "file-overlap":
            return []
        if any(row["number"] == ready["issue"] and _time(ready["at"]) <= row["started"] <= low
               for row in executions):
            return []
    return overlap["data"].get("holds", [])


def parts(approved: datetime, merged: datetime, phases: list[tuple[str, Interval]],
          records: list[dict[str, Any]], executions: list[dict[str, Any]],
          first_pass: datetime | None = None, *, failures=(), batches=()) -> dict[str, float]:
    activities = _activities(records)
    failures = [*failures, *(_time(row["at"]) for row in records if row["kind"] == "ci-failure")]
    failures = [at for at in failures if at is not None]
    spans = _spans(phases, activities, approved, merged, failures, first_pass)
    points = {approved, merged}
    points.update(at for _, span, _ in spans for at in span)
    points.update(at for span, _ in batches for at in span)
    points.update(at for _, span, _ in activities for at in span)
    points.update(at for row in records if (at := _time(row["at"])) is not None)
    points.update(at for row in executions for at in (row["started"], row["finished"]) if at is not None)
    if first_pass is not None:
        points.add(first_pass)
    points = sorted(at for at in points if approved <= at <= merged)
    totals = dict.fromkeys(PARTS, 0.0)
    for low, high in zip(points, points[1:]):
        covering = [span for span in spans if span[1][0] <= low and high <= span[1][1]]
        part = covering[-1][2] if covering else "wasted_waiting"
        # Admission holds have no run identity or phase yet. A wait phase does
        # not mask them; an observed work/merge span does.
        if not covering or covering[-1][0] not in WORK_PHASES | {"merging"}:
            holds = _overlap_holds(records, executions, low)
            if holds:
                part = "necessary_waiting" if _peer_running(holds, executions, low, high) else "wasted_waiting"
        batch = [part for (a, b), part in batches if a <= low and high <= b]
        # A recorded work or merging interval is evidence; the batch CI interval is inferred.
        if batch and not (covering and covering[-1][0] in WORK_PHASES | {"merging"}):
            part = batch[-1]
        totals[part] += (high - low).total_seconds() / 60
    return totals


def _batch_spans(records, number):
    """Each batch's tip CI for one member, ending before the owner's own work on that batch.

    A ``merge-batch`` record is written at retirement, after the owner's final checks and
    the landing push, so its time only bounds the end of tip CI. The owner's final checks
    start after tip CI passed, so the member's first merge-owner check or merge after the previous
    batch it belonged to ends this batch's CI interval instead."""
    owner = sorted(start for row in records
                   if row["kind"] == "activity" and row.get("issue") == number
                   and row["data"].get("source") == "merge-owner"
                   and row["data"].get("activity") in {"checking", "merging"}
                   and (start := _time(row["data"].get("started_at"))) is not None)
    batches = sorted((at, row["data"]) for row in records
                     if row["kind"] == "merge-batch" and (at := _time(row["at"])) is not None
                     and any(member.get("n") == number for member in row["data"].get("members", [])))
    spans, before = [], None
    for at, data in batches:
        index = next(i for i, member in enumerate(data.get("members", [])) if member.get("n") == number)
        if data.get("ci_minutes"):
            used = data.get("result") == "used" and index < data.get("used_members", 0)
            end = min([at, *(start for start in owner if (before is None or start > before) and start <= at)])
            spans.append(((end - timedelta(minutes=data["ci_minutes"]), end), "value_work" if used else "wasted_work"))
        before = at
    return spans


def utilization(executions, budgets, now: datetime, days: int) -> list[float | None]:
    """Run-minutes / available lane-slot minutes, including ongoing executions.

    A running execution holds one slot, even during a wait. The recorded budget
    cannot make fewer slots available than are actually running.
    """
    out = []
    for day in range(days):
        low, high = now - (day + 1) * DAY, now - day * DAY
        points = {low, high}
        points.update(at for row in executions for at in (row["started"], row["finished"] or now)
                      if low < at < high)
        points.update(at for at, _ in budgets if low < at < high)
        busy = available = 0.0
        points = sorted(points)
        for a, b in zip(points, points[1:]):
            running = sum(row["started"] <= a and b <= (row["finished"] or now) for row in executions)
            minutes = (b - a).total_seconds() / 60
            busy += running * minutes
            available += max(_value_at(budgets, a) or 0, running) * minutes
        out.append(busy / available if available else None)
    return out


def windows(store: StateStore, now: datetime, days: int, issues, executions, phases):
    records, reviews = store.flow_records(), orch_events.events(store, "review")
    usage = utilization(executions, _budgets(store, now)["parallel"], now, days)
    out = [{"median": None, "issues": [], "utilization": value} for value in usage]
    merges = {(row["number"], row["merged_at"]): row for row in executions
              if row["merged"] and row["merged_at"] is not None and row["merged_at"] <= now}
    for number, merged in sorted(merges, key=lambda key: (key[1], key[0])):
        day = int((now - merged) // DAY)
        if day >= days or (approved := approval_before(store, issues, number, merged)) is None:
            continue
        first_pass = min((at for event in reviews if event.get("issue") == number and event.get("verdict") == "PASS"
                          and (at := _time(event.get("at"))) is not None and approved <= at <= merged), default=None)
        failures = [_time(event.get("at")) for event in reviews
                    if event.get("issue") == number and event.get("verdict") == "FAIL"]
        value = parts(approved, merged, [(name, span) for issue, name, span in phases if issue == number],
                      [row for row in records if row["issue"] == number], executions, first_pass,
                      failures=failures, batches=_batch_spans(records, number))
        denominator = value["value_work"] + value["wasted_work"] + value["wasted_waiting"]
        out[day]["issues"].append({"issue": number, **value,
                                   "efficiency": value["value_work"] / denominator if denominator else None})
    for day in out:
        day["median"] = mid(entry["efficiency"] for entry in day["issues"])
    return out


def previous(days):
    return {"median": mid(day["median"] for day in days),
            "issues": [entry for day in days for entry in day["issues"]],
            "utilization": mid(day["utilization"] for day in days)}

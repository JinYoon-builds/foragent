"""Approval-to-merge accounting for `fa orch scorecard`: six parts plus unknown, in issue-minutes.

Each confirmed merge counts once, from its issue's latest approval at or before the merge to
the merge itself, in the window that holds the merge. The interval is cut at every moment a
retained local fact starts or stops. A piece takes a part only when every fact covering it
names that same part; no fact, or facts naming different parts, leave it unknown. There is
no precedence between parts, and nothing is inferred from today's issue state.

Facts and the part they name:

* ``activity`` records, by their recorded cause: ``initial`` is work; ``base-changed``,
  ``group-failed`` and ``file-overlap`` are concurrency cost; ``queue`` is waste.
* Phase events, only where no recorded fact covers the time: the first implementing,
  reviewing, checking, ci or merging phase of the interval is work (later ones need an
  activity cause); an overlap wait is concurrency cost.
* Failure facts: a later implementing, reviewing, checking, ci or resolving-conflict phase that
  starts at or after a review FAIL (``review`` journal event) or a ``ci-failure`` record, and
  before the end of the failed run, is rework. A later round no recorded failure caused says
  nothing, and a recorded activity cause still wins over this phase-name inference.
* ``ready`` records, from each record to the issue's next ready record or run start:
  ``slots-full`` with merge waiters holding slots is waste, without them and with the
  measured occupancy at the limit it is machine limit; ``free-slot`` and ``held-no-retry``
  are waste; ``blocked-by`` is ordering; ``file-overlap`` is concurrency cost.
* ``issue-state`` records with an open native blocked_by edge are ordering until a later
  record of the issue shows no open edge.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from foragent import orch_events, orch_scorecard_history as history
from foragent.orch_facts_resources import _time
from foragent.orch_scorecard_records import Interval, approval_before
from foragent.state import StateStore

PARTS = ("work", "rework", "concurrency_cost", "machine_limit", "ordering", "waste")
CAUSES = {"initial": "work", "base-changed": "concurrency_cost", "group-failed": "concurrency_cost",
          "file-overlap": "concurrency_cost", "queue": "waste"}
WORK_PHASES = {"implementing", "reviewing", "checking", "ci", "merging"}
REWORK_PHASES = {"implementing", "reviewing", "checking", "ci", "resolving-conflict"}
PHASE_PARTS = {"overlap-wait": "concurrency_cost"}
READY_PARTS = {"free-slot": "waste", "held-no-retry": "waste", "blocked-by": "ordering",
               "file-overlap": "concurrency_cost"}
DAY = timedelta(hours=24)


def _slots_part(slots: Any) -> str | None:
    """The part a measured full run slot names; an unmeasured one names none."""
    if not isinstance(slots, dict) or not all(type(slots.get(key)) is int
                                              for key in ("limit", "occupied", "merge_waiters")):
        return None
    if slots["merge_waiters"] > 0:
        return "waste"  # Ready work denied productive capacity by merge waiters.
    return "machine_limit" if slots["occupied"] >= slots["limit"] else None


def _ready_facts(records: list[dict[str, Any]], starts: list[datetime], end: datetime) -> list[tuple[Interval, str]]:
    """Each waiting ready record's part, held until the next ready record or run start."""
    rows = [(at, record["data"]) for record in records if record["kind"] == "ready"
            and (at := _time(record["at"])) is not None]
    facts = []
    for index, (at, data) in enumerate(rows):
        if data.get("outcome") != "waiting":
            continue
        stops = [moment for moment in [*(later for later, _ in rows[index + 1:]), *starts] if moment > at]
        reason = data.get("reason")
        part = _slots_part(data.get("run_slots")) if reason == "slots-full" else READY_PARTS.get(reason)
        if part is not None:
            facts.append(((at, min([*stops, end])), part))
    return facts


def _ordering_facts(records: list[dict[str, Any]], end: datetime) -> list[tuple[Interval, str]]:
    """Recorded open dependencies, each until a later record read the edges again (one shared reader)."""
    rows = [(at, record["data"]) for record in records if record["kind"] == "issue-state"
            and (at := _time(record["at"])) is not None]
    return [((start, end if stop is None else stop), "ordering") for start, stop in history.blocked_spans(rows)]


def _activity_facts(records: list[dict[str, Any]]) -> list[tuple[Interval, str]]:
    facts = []
    for record in records:
        data = record["data"]
        start, stop = _time(data.get("started_at")), _time(data.get("finished_at"))
        if record["kind"] == "activity" and start and stop and data.get("cause") in CAUSES:
            facts.append(((start, stop), CAUSES[data["cause"]]))
    return facts


def _failure_spans(number: int, records: list[dict[str, Any]], reviews: list[dict[str, Any]],
                   runs: list[tuple[datetime, datetime | None]], end: datetime) -> list[Interval]:
    """Each recorded failure of the issue, from its moment to the end of the run it failed.

    A run ends when it finished, else when the issue's next run started, else at ``end``. A CI
    failure names its run; a review FAIL belongs to the latest run started at or before it."""
    runs = sorted(runs)
    ends = {start: finished or next((later for later, _ in runs if later > start), end)
            for start, finished in runs}
    failures = [(at, _time(record.get("run_started_at"))) for record in records
                if record["kind"] == "ci-failure" and (at := _time(record["at"])) is not None]
    for event in reviews:
        if event.get("issue") == number and event.get("verdict") == "FAIL" and (at := _time(event.get("at"))):
            failures.append((at, max((start for start, _ in runs if start <= at), default=None)))
    return [(at, ends[start]) for at, start in failures if start in ends and start <= at < ends[start]]


def _phase_facts(phases: list[tuple[str, Interval]], approved: datetime,
                 failures: list[Interval] = ()) -> list[tuple[Interval, str]]:
    """First work phases of the interval are work; a later one is rework only after a recorded failure."""
    facts, seen = [], set()
    for phase, span in sorted(phases, key=lambda item: item[1][0]):
        if span[1] <= approved:
            continue
        first = phase not in seen
        seen.add(phase)
        if phase in WORK_PHASES and first:
            facts.append((span, "work"))
        elif phase in REWORK_PHASES and not first and any(a <= span[0] < b for a, b in failures):
            facts.append((span, "rework"))
        elif phase in PHASE_PARTS:
            facts.append((span, PHASE_PARTS[phase]))
    return facts


def split(approved: datetime, merged: datetime, facts: list[tuple[Interval, str]],
          inferred: list[tuple[Interval, str]] = ()) -> dict[str, float]:
    """Minutes of [approved, merged] per part; a piece covered by no part or several is unknown.

    ``inferred`` parts (from phase names alone) explain only pieces no recorded fact covers:
    recorded provenance decides, so a recheck's cause is never contradicted by its phase name."""
    points = sorted({approved, merged, *(moment for (a, b), _ in (*facts, *inferred) for moment in (a, b)
                                         if approved < moment < merged)})
    parts = dict.fromkeys((*PARTS, "unknown"), 0.0)
    for low, high in zip(points, points[1:]):
        named = {part for (a, b), part in facts if a <= low and high <= b}
        named = named or {part for (a, b), part in inferred if a <= low and high <= b}
        parts[named.pop() if len(named) == 1 else "unknown"] += (high - low).total_seconds() / 60
    return parts


def windows(store: StateStore, now: datetime, days: int, issues: dict[int, dict[str, Any]],
            executions: list[dict[str, Any]], phases: list[tuple[int | None, str, Interval]],
            ) -> list[dict[str, Any]]:
    """Each bucket's additive cohort total, today first, with its missing coverage."""
    out = [{"total_minutes": 0.0, "parts": dict.fromkeys((*PARTS, "unknown"), 0.0),
            "merges": 0, "missing_approval": 0} for _ in range(days)]
    records = store.flow_records()  # Bounded retained evidence; older facts may explain the interval.
    reviews = orch_events.events(store, "review")
    merges = {}
    for row in executions:
        if row["merged"] and row["merged_at"] is not None and row["merged_at"] <= now:
            merges.setdefault((row["number"], row["merged_at"]), row)  # One merge counts once.
    for (number, merged), _ in sorted(merges.items()):
        bucket = int((now - merged) // DAY)
        if bucket >= days:
            continue
        window = out[bucket]
        approved = approval_before(store, issues, number, merged)
        if approved is None:
            window["missing_approval"] += 1
            continue
        own = [record for record in records if record["issue"] == number]
        starts = [row["started"] for row in executions if row["number"] == number]
        facts = _activity_facts(own) + _ready_facts(own, starts, merged) + _ordering_facts(own, merged)
        runs = [(row["started"], row["finished"]) for row in executions if row["number"] == number]
        failures = _failure_spans(number, own, reviews, runs, merged)
        parts = split(approved, merged, facts, _phase_facts(
            [(phase, span) for issue, phase, span in phases if issue == number], approved, failures))
        window["merges"] += 1
        window["total_minutes"] += (merged - approved).total_seconds() / 60
        for part, minutes in parts.items():
            window["parts"][part] += minutes
    return out


def previous(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """The earlier days' cohort total (a sum, not a median)."""
    total = {"total_minutes": 0.0, "parts": dict.fromkeys((*PARTS, "unknown"), 0.0),
             "merges": 0, "missing_approval": 0}
    for window in windows:
        for key in ("total_minutes", "merges", "missing_approval"):
            total[key] += window[key]
        for part, minutes in window["parts"].items():
            total["parts"][part] += minutes
    return total

"""D96 flow metrics for `fa orch scorecard`: idle time, waste, waits, lead time, person interventions.

Every metric is worse when higher. Each one is computed per 24-hour bucket (the same
buckets as orch_scorecard: bucket k holds ``now - (k+1) day < time <= now - k day``);
intervals are clipped to the buckets they overlap. Only local records are read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

from foragent import capacity
from foragent.labels import issue_lane
from foragent.orch_facts_resources import _jsonl, _time
from foragent.state import StateStore

DAY = timedelta(hours=24)
# Wait phases by cause; the runner's phase names stay the source of truth.
CAUSES = {"test-slot-wait": "slot", "overlap-wait": "overlap", "merge-turn-wait": "merge",
          "merge-lock-wait": "merge", "executor-wait": "quota", "limit-wait": "quota"}
CAUSE_ORDER = ("slot", "overlap", "merge", "quota")
TEST_PHASES = {"checking"}  # The phase that holds a test slot.
INTERVENTIONS = {"ask", "escalate", "person-found"}
HOLDING = {"blocked", "needs-human", "running"}  # Labels that keep an approved issue from starting.
NAMES = ("idle_minutes", "waste_share", "wait_minutes_by_cause", "lead_time_minutes", "person_interventions")
Interval = tuple[datetime, datetime]


def _minutes(start: datetime, end: datetime, now: datetime, days: int) -> list[float]:
    """Minutes of [start, end] in each bucket, up to now."""
    out = [0.0] * days
    end = min(end, now)
    for bucket in range(days):
        high, low = now - bucket * DAY, now - (bucket + 1) * DAY
        overlap = (min(end, high) - max(start, low)).total_seconds() / 60
        if overlap > 0:
            out[bucket] = overlap
    return out


def _bucket(moment: datetime | None, now: datetime, days: int) -> int | None:
    if moment is None or moment > now:
        return None
    index = (now - moment) // DAY
    return index if index < days else None


def _phase_intervals(store: StateStore, now: datetime) -> list[tuple[int | None, str, Interval]]:
    """(issue, phase, interval) for closed phase events (a replay counts once) and open run phases."""
    unique: dict[Any, tuple[int | None, str, Interval]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        opened, closed = _time(event.get("phase_started_at")), _time(event.get("at"))
        if event.get("kind") == "phase" and isinstance(event.get("phase"), str) and opened and closed:
            unique[(event.get("issue"), event.get("phase_started_at"))] = (
                event.get("issue"), event["phase"], (opened, closed))
    for run in store.active_runs():
        opened = _time(run.phase_started_at)
        if run.status == "running" and run.phase and opened:
            unique.setdefault((run.number, run.phase_started_at), (run.number, run.phase, (opened, now)))
    return list(unique.values())


def _executions(store: StateStore, now: datetime) -> list[dict[str, Any]]:
    """Every execution with its runtime interval; a finished one also says whether it merged and when."""
    finished_at, merged_at = store.finish_times(), store.merge_times()
    rows = []
    for run in store.read_history():
        started = _time(run.started_at)
        if run.status != "finished" or started is None:
            continue
        key = (run.number, run.started_at)
        finished = _time(finished_at.get(key)) or _time(run.last_progress_at) or started
        merge = _time(merged_at.get(key))
        merge = merge if merge is not None and merge >= started else None
        merged = run.outcome == "merged" or (run.outcome == "pr-open" and merge is not None)
        rows.append({"number": run.number, "started": started, "finished": finished, "merged": merged,
                     "merged_at": (merge or finished) if merged else None})
    done = {(row["number"], row["started"]) for row in rows}
    for run in store.active_runs():
        started = _time(run.started_at)
        if run.status == "running" and started and (run.number, started) not in done:
            rows.append({"number": run.number, "started": started, "finished": None, "merged": False,
                         "merged_at": None})
    return rows


def _budgets(now: datetime) -> dict[str, list[tuple[datetime, int]]]:
    """Each shared budget's recorded values over time; before the first record it is unknown."""
    rows: dict[str, list[tuple[datetime, int]]] = {"parallel": [], "test_slots": []}
    for row in _jsonl(capacity.machine_root() / capacity.HISTORY_FILE):
        at = _time(row.get("at"))
        if row.get("budget") in rows and at is not None and at <= now and type(row.get("to")) is int:
            rows[row["budget"]].append((at, row["to"]))
    return {name: sorted(values) for name, values in rows.items()}


def _value_at(values: list[tuple[datetime, int]], moment: datetime) -> int | None:
    current = None
    for at, value in values:
        if at > moment:
            break
        current = value
    return current


def _pauses(store: StateStore) -> dict[str, list[Interval]]:
    """Plan pause intervals per lane: from the pause to its until, a resume or a plan expiry."""
    paused: dict[str, list[list[datetime]]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        at, kind, lane = _time(event.get("at")), event.get("kind"), event.get("lane")
        if at is None:
            continue
        if kind == "plan-pause" and (until := _time(event.get("until"))) is not None:
            paused.setdefault(lane, []).append([at, until])
        elif kind in ("plan-resume", "plan-expired"):
            for name, spans in paused.items():
                if kind == "plan-expired" or name == lane:
                    for span in spans:
                        if span[0] <= at < span[1]:
                            span[1] = at
    return {lane: [(start, end) for start, end in spans] for lane, spans in paused.items()}


def _inside(spans: list[Interval], moment: datetime) -> bool:
    return any(start <= moment < end for start, end in spans)


def _issue_view(store: StateStore, row: dict[str, Any], lanes: dict) -> dict[str, Any] | None:
    """What the idle and lead-time rules need to know about one local issue."""
    events = sorted(((at, item.get("label"), item.get("action"))
                     for item in ((row.get("reads") or {}).get("label_events") or {}).get("value") or []
                     if isinstance(item, dict) and (at := _time(item.get("created_at"))) is not None),
                    key=lambda event: event[0])
    approval = store.load_approval(row["number"])
    approved_at = _time(approval.approved_at) if approval else None
    closed = row.get("state") != "open"
    blockers = [item for item in row.get("blocked_by") or [] if isinstance(item, dict)]
    return {"events": events, "approved_at": approved_at, "labels": set(row.get("labels") or ()),
            "open_until": (_time(row.get("merged_at")) if closed else None), "closed": closed,
            "blocked": any(item.get("state") != "closed" for item in blockers),
            "lane": issue_lane(row.get("labels") or (), lanes)}


def _labels_at(issue: dict[str, Any], moment: datetime) -> set[str]:
    if not issue["events"]:  # No label history: the current labels hold from the approval on.
        return issue["labels"] if issue["approved_at"] and issue["approved_at"] <= moment else set()
    labels: set[str] = set()
    for at, label, action in issue["events"]:
        if at > moment:
            break
        (labels.add if action == "labeled" else labels.discard)(label)
    return labels


def _startable(issue: dict[str, Any], moment: datetime, pauses: dict[str, list[Interval]]) -> bool:
    if issue["blocked"] or (issue["closed"] and (issue["open_until"] is None or moment >= issue["open_until"])):
        return False
    labels = _labels_at(issue, moment)
    return ({"approved", "ready"} <= labels and not labels & HOLDING
            and not _inside(pauses.get(issue["lane"], []), moment))


def _latest_approval(issue: dict[str, Any], moment: datetime) -> datetime | None:
    times = [at for at, label, action in issue["events"]
             if label == "approved" and action == "labeled" and at <= moment]
    if issue["approved_at"] is not None and issue["approved_at"] <= moment:
        times.append(issue["approved_at"])
    return max(times, default=None)


def _idle(store: StateStore, now: datetime, days: int, issues: dict[int, dict[str, Any]],
          executions: list[dict[str, Any]], phases: list[tuple[int | None, str, Interval]]) -> list[float]:
    """Wall minutes with a startable approved issue while a run or test slot sat unused."""
    budgets, pauses = _budgets(now), _pauses(store)
    occupied: list[tuple[int, Interval]] = [(row["number"], (row["started"], row["finished"] or now))
                                            for row in executions]
    for number, prepared in store.run_starts().items():  # A prepared start reserves its slot.
        if (at := _time(prepared)) is not None:
            later = [row["started"] for row in executions if row["number"] == number and row["started"] >= at]
            occupied.append((number, (at, min(later, default=now))))
    testing = [span for _, phase, span in phases if phase in TEST_PHASES]
    start = now - days * DAY
    points = {start, now}
    for spans in [[span for _, span in occupied], testing, *pauses.values()]:
        points.update(moment for span in spans for moment in span)
    points.update(at for values in budgets.values() for at, _ in values)
    for issue in issues.values():
        points.update(at for at, _, _ in issue["events"])
        points.update(at for at in (issue["approved_at"], issue["open_until"]) if at is not None)
    ordered = sorted(moment for moment in points if start <= moment <= now)
    idle = [0.0] * days
    for low, high in zip(ordered, ordered[1:]):
        moment = low + (high - low) / 2
        busy = {number for number, (a, b) in occupied if a <= moment < b}
        if not any(number not in busy and _startable(issue, moment, pauses) for number, issue in issues.items()):
            continue
        parallel, slots = _value_at(budgets["parallel"], moment), _value_at(budgets["test_slots"], moment)
        testing_now = sum(1 for a, b in testing if a <= moment < b)
        running_now = sum(1 for _, (a, b) in occupied if a <= moment < b)
        if (parallel is not None and running_now < parallel) or (slots is not None and testing_now < slots):
            idle = [total + part for total, part in zip(idle, _minutes(low, high, now, days))]
    return idle


def daily(store: StateStore, now: datetime, days: int, lanes: dict | None = None) -> list[dict[str, Any]]:
    """Each bucket's flow metrics, today first."""
    issues = {row["number"]: view for row in store.local_issues()
              if (view := _issue_view(store, row, lanes or {})) is not None}
    executions, phases = _executions(store, now), _phase_intervals(store, now)
    runtime, wasted = [0.0] * days, [0.0] * days
    leads: list[list[float]] = [[] for _ in range(days)]
    merges = [0] * days
    for row in executions:
        if row["finished"] is None or row["finished"] > now:
            continue  # Waste counts ended runtime only.
        part = _minutes(row["started"], row["finished"], now, days)
        runtime = [a + b for a, b in zip(runtime, part)]
        if not row["merged"]:
            wasted = [a + b for a, b in zip(wasted, part)]
        elif (bucket := _bucket(row["merged_at"], now, days)) is not None:
            merges[bucket] += 1
            issue = issues.get(row["number"])
            if issue and (approved := _latest_approval(issue, row["merged_at"])) is not None:
                leads[bucket].append((row["merged_at"] - approved).total_seconds() / 60)
    waits = [dict.fromkeys(CAUSE_ORDER, 0.0) for _ in range(days)]
    for _, phase, (opened, closed) in phases:
        if phase in CAUSES:
            for bucket, minutes in enumerate(_minutes(opened, closed, now, days)):
                waits[bucket][CAUSES[phase]] += minutes
    people = [0] * days
    for event in _jsonl(store.root / "orch-events.jsonl"):
        if event.get("kind") in INTERVENTIONS and (bucket := _bucket(_time(event.get("at")), now, days)) is not None:
            people[bucket] += 1
    idle = _idle(store, now, days, issues, executions, phases)
    return [{"merges": merges[k], "idle_minutes": idle[k],
             "waste_share": wasted[k] / runtime[k] if runtime[k] else None,
             "wait_minutes_by_cause": waits[k],
             "lead_time_minutes": median(leads[k]) if leads[k] else None,
             "person_interventions": people[k]} for k in range(days)]


def previous(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Daily medians of the earlier buckets; a metric unknown on a day leaves that day out."""
    def mid(values):
        values = [value for value in values if value is not None]
        return median(values) if values else None

    result = {name: mid([day[name] for day in days]) for name in ("merges", *NAMES) if name != "wait_minutes_by_cause"}
    result["wait_minutes_by_cause"] = {cause: mid([day["wait_minutes_by_cause"][cause] for day in days]) or 0
                                       for cause in CAUSE_ORDER}
    for name in ("idle_minutes", "person_interventions"):
        result[name] = result[name] or 0
    return result


def worse_names(today: dict[str, Any], before: dict[str, Any]) -> list[str]:
    """The flow metrics that are higher today; merges are context, never a target."""
    pairs = [(name, today[name], before[name]) for name in NAMES if name != "wait_minutes_by_cause"]
    pairs += [(f"wait_minutes_by_cause.{cause}", today["wait_minutes_by_cause"][cause],
               before["wait_minutes_by_cause"][cause]) for cause in CAUSE_ORDER]
    return [name for name, now, then in pairs if now is not None and then is not None and now > then]

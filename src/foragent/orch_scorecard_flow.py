"""D96 flow metrics for `fa orch scorecard`: idle time, waste, waits, lead time, person interventions.

Every metric is worse when higher. Each one is computed per 24-hour bucket (the same
buckets as orch_scorecard: bucket k holds ``now - (k+1) day < time <= now - k day``);
intervals are clipped to the buckets they overlap. Only local records are read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from types import SimpleNamespace
from typing import Any

from foragent import capacity
from foragent.orch_facts_resources import _jsonl, _time
from foragent.orch_scorecard_records import (
    Interval, TEST_PHASES, _budgets, _executions, _inside, _issue_view, _issues, _machine_at, _occupancy,
    _pauses, _phase_intervals, _run_value, _siblings, _value_at,
)
from foragent.state import StateStore

DAY = timedelta(hours=24)
# Wait phases by cause; the runner's phase names stay the source of truth.
CAUSES = {"test-slot-wait": "slot", "overlap-wait": "overlap", "merge-turn-wait": "merge",
          "merge-lock-wait": "merge", "executor-wait": "quota", "limit-wait": "quota"}
CAUSE_ORDER = ("slot", "overlap", "merge", "quota")
INTERVENTIONS = {"ask", "escalate", "person-found"}
HOLDING = {"blocked", "needs-human", "running"}  # Labels that keep an approved issue from starting.
NAMES = ("idle_minutes", "waste_share", "wait_minutes_by_cause", "lead_time_minutes", "person_interventions")


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


def _labels_at(issue: dict[str, Any], moment: datetime) -> set[str]:
    if not issue["events"]:
        # No label history: startable from its approval (D96). A current label is applied only
        # from a recorded time: a hold from its flag time, else (like a removed approval) from
        # the issue's recorded updated_at, which is at or after the change. With no time it is
        # not applied, so today's labels never rewrite earlier days. Own runs come from run records.
        if issue["approved_at"] is None or moment < issue["approved_at"]:
            return set()
        if not issue["closed"] and "approved" not in issue["labels"] and not issue.get("running"):
            # Approval removed at an unrecorded time and no run of its own: never startable in the
            # window (the scorecard is a diagnostic; a wrong minute is reversible).
            return set()
        labels = {"approved", "ready"}
        held = issue["labels"] & {"blocked", "needs-human"}
        if held and (since := issue["flagged_at"] or issue["updated_at"]) is not None and moment >= since:
            labels |= held
        if (not issue["closed"] and "approved" not in issue["labels"] and issue["updated_at"] is not None
                and moment >= issue["updated_at"]):
            labels.discard("approved")
        return labels
    labels: set[str] = set()
    for at, label, action in issue["events"]:
        if at > moment:
            break
        (labels.add if action == "labeled" else labels.discard)(label)
    return labels


def _facts_labels_at(issue: dict[str, Any], moment: datetime) -> set[str]:
    """The labels fa orch facts would read: label events where recorded, else the current labels.

    Facts reads only current labels, so without label events no ``ready`` is assumed (that
    is idle's rule) and a current ``running`` label still holds the issue."""
    if issue["events"]:
        return _labels_at(issue, moment)
    return set(issue["labels"]) if issue["approved_at"] is not None and moment >= issue["approved_at"] else set()


def _startable(issue: dict[str, Any], moment: datetime, pauses: dict[str, list[Interval]], *,
               facts: bool = False) -> bool:
    """Approved, ready and open at ``moment``, not held by a label or a plan pause in ``pauses``.

    ``facts=True`` is facts' waiting list: no open-blocker rule and facts' labels."""
    if not facts and any(until is None or moment < until for until in issue["blocked_until"]) or (issue["closed"] and (issue["open_until"] is None or moment >= issue["open_until"])):
        return False
    labels = (_facts_labels_at if facts else _labels_at)(issue, moment)
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
    budgets, pauses = _budgets(store, now), _pauses(store)
    occupied, testing = _occupancy(store, now, executions, phases)
    start = now - days * DAY
    points = {start, now}
    for spans in [[span for _, span in occupied], testing, *pauses.values()]:
        points.update(moment for span in spans for moment in span)
    for row in executions:
        points.update((row["started"], row["finished"] or now))
    points.update(at for values in budgets.values() for at, _ in values)
    for issue in issues.values():
        points.update(at for at, _, _ in issue["events"])
        points.update(at for at in (issue["approved_at"], issue["open_until"], issue["flagged_at"], issue["updated_at"],
                                    *issue["blocked_until"]) if at is not None)
    ordered = sorted(moment for moment in points if start <= moment <= now)
    idle = [0.0] * days
    for low, high in zip(ordered, ordered[1:]):
        moment = low + (high - low) / 2
        busy = {number for (index, number), (a, b) in occupied if not index and a <= moment < b}
        busy |= {row["number"] for row in executions if row["started"] <= moment < (row["finished"] or now)}
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
    issues = _issues(store, lanes or {})
    executions, phases = _executions(store, now), _phase_intervals(store, now)
    runtime, wasted = [0.0] * days, [0.0] * days
    leads: list[list[float]] = [[] for _ in range(days)]
    merges = [0] * days
    for row in executions:
        ended = row["finished"] is not None and row["finished"] <= now
        part = _minutes(row["started"], row["finished"] or now, now, days)
        runtime = [a + b for a, b in zip(runtime, part)]  # Ongoing runtime is runtime too.
        if ended and not row["merged"]:
            wasted = [a + b for a, b in zip(wasted, part)]  # Only runtime that ended without a merge is waste.
        if not row["merged"]:
            continue
        if (bucket := _bucket(row["merged_at"], now, days)) is not None:
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


def binding_at(store: StateStore, config: Any, root, moment: datetime, *, current: bool) -> str:
    """fa orch facts' binding replayed read-only from the local records in force at ``moment``.

    The start loop is facts' own (_admission): its start order (``_ranked``), lane holds,
    then the gate's file rule (``admits``: lane serialisation, exclusive reservations,
    overlap_per_file) against the runs that held slots then; ``_binding`` then names the
    constraint. A phase that closed exactly at ``moment`` still counts, so a day's end sees
    the wait that ran up to it. Usage, lane and GitHub pauses keep only their end time, so
    they count only when ``current`` (moment is now); plan pauses are journaled with times.
    """
    from foragent import machine_budget, orch_queue
    from foragent.config import in_run_hours, lane_executors
    from foragent.issue import parse_body
    from foragent.orch_facts_resources import _binding
    from foragent.overlap_files import Claim, admits, approved_issue, claim, recorded_union
    from foragent.scheduler_start import _ranked
    from foragent.state import Run

    ctx = SimpleNamespace(store=store, config=config, repo_root=root, now=lambda: moment)
    issues = _issues(store, config.lanes)
    executions, phases = _executions(store, moment), _phase_intervals(store, moment)
    occupied, _ = _occupancy(store, moment, executions, phases, open_end=moment + 3650 * DAY)
    holding = [(index, number) for (index, number), (a, b) in occupied if a <= moment < b]
    busy = {number for index, number in holding if not index}
    # Waiting runs retain their project place and file claims even while their machine slot is free.
    busy |= {row["number"] for row in executions if row["started"] <= moment < (row["finished"] or moment + DAY)}
    runs = {(run.number, _time(run.started_at)): run for run in [*store.read_history(), *store.active_runs()]}
    known, unknown = [], []
    for row in executions:  # This repository's runs holding a slot at the moment claim their Files.
        run = runs.get((row["number"], row["started"]))
        end = row["finished"] or moment + DAY
        if run is None or not row["started"] <= moment < end:
            continue
        parsed = approved_issue(ctx, run)
        if parsed is None or not parsed.files:
            unknown.append(run)
        else:
            known.append(claim(run, parsed))  # facts' claims(): committed measured files first
    open_phases = {(number, phase) for number, phase, (a, b) in phases if a <= moment <= b}
    pauses = _pauses(store)
    # Facts' candidates (_waiting_issues, then startable): an approved snapshot, and no blocker
    # rule; plan and usage pauses hold them inside the loop below.
    approvals = {number: approval for number in issues if (approval := store.load_approval(number)) is not None}
    lanes: dict[str, list[int]] = {}
    for number, issue in sorted(issues.items()):
        if number in approvals and number not in busy and _startable(issue, moment, {}, facts=True):
            lanes.setdefault(issue["lane"], []).append(number)
    ready = {lane: orch_queue.ordered(store, lane, numbers) for lane, numbers in lanes.items()}
    ranked = _ranked(ctx, ready, claims=list(known))
    github = current and store.github_wait_until(moment) is not None
    outside = not in_run_hours(moment.astimezone(), config.run_hours)
    budgets, machine = _budgets(store, moment), machine_budget.values()
    pressure, load = _machine_at(moment)
    parallel = _value_at(budgets["parallel"], moment)  # Facts' default when nothing is recorded yet.
    cap = parallel if parallel is not None else capacity.INITIAL_PARALLEL
    if (ceiling := machine["max_parallel"]) is not None:
        cap = min(cap, ceiling)
    free = max(0, cap - len(holding))
    if config.max_parallel is not None:
        free = min(free, max(0, config.max_parallel - len(busy)))
    held: dict[int, str] = {}
    remaining, union = free, recorded_union(store)
    for position, (lane, number) in enumerate(ranked):
        parsed = parse_body(approvals[number].body)
        if github or outside:
            held[number] = "github" if github else "run_hours"
        elif remaining <= 0:
            held.update({later: "cap" for _, later in ranked[position:]})
            break
        elif _inside(pauses.get(lane, []), moment) or current and (store.lane_paused(lane, moment) or any(
                store.executor_paused(name, moment) for name in lane_executors(config, lane))):
            held[number] = "quota"
        elif not admits(ctx, lane, parsed, known, unknown, union=union):
            held[number] = "structure"
        else:
            remaining -= 1
            started = Run(number, "", "", None, moment.isoformat(), moment.isoformat(), lane=lane)
            if parsed is not None and parsed.files:
                known.append(Claim(started, parsed, parsed.files))
            else:
                unknown.append(started)
    resources = {
        # Facts' rule over the recorded sample: critical memory, or load at OVERLOAD_LOAD.
        "machine": {"overloaded": pressure == "critical" or load >= capacity.OVERLOAD_LOAD,
                    "load_per_cpu": load, "memory_pressure": pressure},
        "test_slots": {"queue_length": sum(phase == "test-slot-wait" for _, phase in open_phases),
                       "admitted": capacity.effective_test_slots(
                           config.test_slots, machine=machine,
                           state=None if (slots := _value_at(budgets["test_slots"], moment)) is None
                           else {"slots": slots})},
        "parallelizable_work": {
            "waiting_issues": [number for _, number in ranked], "held": {str(n): r for n, r in held.items()},
            "in_flight": {str(n): "structure" for n, phase in open_phases if phase == "overlap-wait"},
            "free_run_slots": free, "github_wait_until": moment.isoformat() if github else None},
    }
    found = _binding(resources)
    return found["kind"] if found else "none"

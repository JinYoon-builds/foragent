"""Read-only local records shared by scorecard flow metrics and binding replay."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from foragent import capacity
from foragent.labels import issue_lane
from foragent.orch_facts_resources import _jsonl, _time
from foragent.state import StateStore

TEST_PHASES = {"checking"}  # The phase that holds a test slot.
Interval = tuple[datetime, datetime]


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
        # By effect, not label: a recorded merge is a merge even if a later step failed.
        merged = run.outcome == "merged" or merge is not None
        rows.append({"number": run.number, "started": started, "finished": finished, "merged": merged,
                     "merged_at": (merge or finished) if merged else None})
    done = {(row["number"], row["started"]) for row in rows}
    for run in store.active_runs():
        started = _time(run.started_at)
        if run.status == "running" and started and (run.number, started) not in done:
            # A merged runner may still be cleaning up: its merge moment is on the running record.
            merge = _time(_run_value(store, run.number).get("merged_at"))
            merge = merge if merge is not None and merge >= started else None
            rows.append({"number": run.number, "started": started, "finished": None, "merged": merge is not None,
                         "merged_at": merge, "pid": run.pid})
    return rows


def _run_value(store: StateStore, number: int) -> dict[str, Any]:
    try:
        value = json.loads((store.root / "runs" / f"{number}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _siblings(store: StateStore) -> list[StateStore]:
    """Every other repository's store under the same data root (the budgets are machine-wide)."""
    try:
        roots = [root for root in store.root.parent.iterdir() if root != store.root and (
            (root / "runs").is_dir() or (root / "run-starts").is_dir() or (root / "runs.jsonl").is_file())]
    except OSError:
        return []
    return [StateStore(root, now=store._now) for root in sorted(roots)]


def _occupancy(store: StateStore, now: datetime, executions: list[dict[str, Any]],
               phases: list[tuple[int | None, str, Interval]], open_end: datetime | None = None,
               ) -> tuple[list[tuple[Any, Interval]], list[Interval]]:
    """Machine-wide run and test-slot holders over time, by fa orch facts' counting rule.

    This store's running records hold a slot through now. Another store's running record
    holds one while its process may live: with a pid through now (liveness is not probed),
    without one only for the launch grace. A prepared start holds one for the grace until
    its run starts. ``open_end`` ends still-open holds (default ``now``); a replay of one
    moment passes a later time so ``start <= moment < end`` keeps them."""
    open_end = open_end or now
    occupied: list[tuple[Any, Interval]] = []
    testing: list[Interval] = []
    for index, (source, rows, spans) in enumerate(
            [(store, executions, phases)] + [(other, _executions(other, now), _phase_intervals(other, now))
                                            for other in _siblings(store)]):
        counted = set()
        for row in rows:
            end = row["finished"] or open_end
            if index and row["finished"] is None and row.get("pid") is None:
                end = min(open_end, row["started"] + capacity.LAUNCH_GRACE)
            occupied.append(((index, row["number"]), (row["started"], end)))
            if row["finished"] is not None or not index or row.get("pid") is not None:
                counted.add(row["number"])
        for number, prepared in source.run_starts().items():
            if (at := _time(prepared)) is not None:
                later = [row["started"] for row in rows if row["number"] == number and row["started"] >= at]
                occupied.append(((index, number), (at, min([*later, at + capacity.LAUNCH_GRACE, open_end]))))
        testing += [span for number, phase, span in spans if phase in TEST_PHASES
                    and (not index or number in counted)]
    return occupied, testing


def _budgets(store: StateStore, now: datetime) -> dict[str, list[tuple[datetime, int]]]:
    """Each shared budget's recorded values over time; before the first record it is unknown.

    Records are the budget history and each budget's current file (parallel-state.json,
    test-slots.json), which holds its value from its own ``changed_at``."""
    rows: dict[str, list[tuple[datetime, int]]] = {"parallel": [], "test_slots": []}
    for row in _jsonl(capacity.machine_root() / capacity.HISTORY_FILE):
        at = _time(row.get("at"))
        if row.get("budget") in rows and at is not None and at <= now and type(row.get("to")) is int:
            rows[row["budget"]].append((at, row["to"]))
    for name, state, key in (("parallel", capacity.read_state(store), "parallel"),
                             ("test_slots", capacity.read_test_slots(), "slots")):
        at = _time((state or {}).get("changed_at"))
        if at is not None and at <= now and type(state.get(key)) is int:
            rows[name].append((at, state[key]))
    return {name: sorted(values) for name, values in rows.items()}


def _machine_at(moment: datetime) -> tuple[str, float]:
    """The memory pressure and load per CPU recorded last at or before ``moment``.

    Every budget-history row (a budget change, or a tick that critical memory held) keeps
    the sample it was decided on. With no row yet, the machine is taken as not overloaded."""
    latest = None
    for row in _jsonl(capacity.machine_root() / capacity.HISTORY_FILE):
        at = _time(row.get("at"))
        if (at is not None and at <= moment and isinstance(row.get("pressure"), str)
                and type(row.get("load_per_cpu")) in (int, float) and (latest is None or at >= latest[0])):
            latest = (at, row["pressure"], float(row["load_per_cpu"]))
    return (latest[1], latest[2]) if latest else ("normal", 0.0)


def _value_at(values: list[tuple[datetime, int]], moment: datetime) -> int | None:
    current = None
    for at, value in values:
        if at > moment:
            break
        current = value
    return current


def _pauses(store: StateStore) -> dict[str, list[Interval]]:
    """Plan pause intervals per lane, replayed from the journal in order.

    Each pause ends at the earliest of its recorded ``until``, the time a later record
    replaced or cleared it (a new pause of the same lane, a resume), and its plan's
    recorded ``expires_at``. The latest ``plan-expires`` of a plan replaces earlier ones;
    ``plan-expired`` carries the value that ended it. The time a tick noticed an expiry is
    never an end. An edit after the expiry starts a fresh plan with no expiry."""
    spans: list[dict[str, Any]] = []
    plan: dict[str, Any] = {"expires": None}  # One record per plan; its pauses share it.
    open_by_lane: dict[Any, dict[str, Any]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        at, kind, lane = _time(event.get("at")), event.get("kind"), event.get("lane")
        if at is None or not str(kind).startswith("plan-"):
            continue
        if kind == "plan-expired":
            if (ends := _time(event.get("expires_at"))) is not None:
                plan["expires"] = ends  # The recorded value of the plan that expired.
            continue
        if plan["expires"] is not None and at >= plan["expires"]:
            plan, open_by_lane = {"expires": None}, {}  # This edit replaced an expired plan.
        if kind == "plan-pause" and (until := _time(event.get("until"))) is not None:
            if (current := open_by_lane.get(lane)) is not None:
                current["stop"] = at
            open_by_lane[lane] = {"lane": lane, "start": at, "until": until, "stop": None, "plan": plan}
            spans.append(open_by_lane[lane])
        elif kind == "plan-resume" and (current := open_by_lane.pop(lane, None)) is not None:
            current["stop"] = at
        elif kind == "plan-expires" and (ends := _time(event.get("expires_at"))) is not None:
            plan["expires"] = ends
    paused: dict[str, list[Interval]] = {}
    for span in spans:
        end = min(moment for moment in (span["until"], span["stop"], span["plan"]["expires"]) if moment is not None)
        if span["start"] < end:
            paused.setdefault(span["lane"], []).append((span["start"], end))
    return paused


def _inside(spans: list[Interval], moment: datetime) -> bool:
    return any(start <= moment < end for start, end in spans)


def _issue_view(store: StateStore, row: dict[str, Any], lanes: dict,
                rows: dict[int, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """What the idle and lead-time rules need to know about one local issue."""
    events = sorted(((at, item.get("label"), item.get("action"))
                     for item in ((row.get("reads") or {}).get("label_events") or {}).get("value") or []
                     if isinstance(item, dict) and (at := _time(item.get("created_at"))) is not None),
                    key=lambda event: event[0])
    approval = store.load_approval(row["number"])
    approved_at = _time(approval.approved_at) if approval else None
    closed = row.get("state") != "open"
    # A blocker blocks while open: an open one throughout (None), a merged one until its merge;
    # a closed one with no recorded close time leaves no evidence that it blocked.
    blocked_until = []
    for item in row.get("blocked_by") or []:
        if not isinstance(item, dict):
            continue
        if item.get("state") != "closed":
            blocked_until.append(None)
        elif (merged := _time(((rows or {}).get(item.get("number")) or {}).get("merged_at"))) is not None:
            blocked_until.append(merged)
    flag = store.load_flag(row["number"])
    return {"events": events, "approved_at": approved_at, "labels": set(row.get("labels") or ()),
            "open_until": (_time(row.get("merged_at")) if closed else None), "closed": closed,
            "blocked_until": blocked_until, "flagged_at": _time(flag.flagged_at) if flag else None,
            "updated_at": _time(row.get("updated_at")),
            "lane": issue_lane(row.get("labels") or (), lanes)}


def _issues(store: StateStore, lanes: dict) -> dict[int, dict[str, Any]]:
    rows = {row["number"]: row for row in store.local_issues()}
    running = {run.number for run in store.active_runs() if run.status == "running"}
    views = {number: view for number, row in rows.items()
             if (view := _issue_view(store, row, lanes, rows)) is not None}
    for number, view in views.items():
        view["running"] = number in running  # An active run record, for the removed-approval rule.
    return views

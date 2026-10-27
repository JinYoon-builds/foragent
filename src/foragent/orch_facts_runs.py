"""Per-execution rows, history, waiting issues and counters for `fa orch facts`.

Everything here computes from the frozen snapshot store that orch_facts.collect reads once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from foragent.config import Config
from foragent.issue import ParsedIssue, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane
from foragent.orch_facts_resources import _jsonl, _seconds, _time
from foragent.state import Run, StateStore
from foragent.state_phases import work_event

RECENT = timedelta(days=7)
PHASES = {"implementing": "implementing_seconds", "checking": "checking_seconds",
          "test-slot-wait": "test_slot_wait_seconds", "baseline": "baseline_seconds"}
PHASE_FIELDS = tuple(PHASES.values())

@dataclass
class _Execution:
    run: Run
    started: datetime
    finished: datetime | None
    events: list[dict[str, Any]] = field(default_factory=list)


def _executions(store: StateStore, now: datetime, current: list[Run]) -> list[_Execution]:
    """Each execution once: completion history first, then current records not finished yet."""
    finished_at = store.finish_times()
    runs: dict[tuple[int, str], Run] = {(run.number, run.started_at): run for run in store.read_history()}
    for run in current:
        runs.setdefault((run.number, run.started_at), run)
    executions = []
    for key, run in runs.items():
        started = _time(run.started_at)
        if started is None or started > now:
            continue  # A future record is not an observation yet.
        finished = _time(finished_at.get(key)) if run.status == "finished" else None
        executions.append(_Execution(run, started, finished))
    executions.sort(key=lambda item: (item.started, item.run.number))
    # Phase events close at their `at`; each belongs to the latest execution of its issue started by then.
    by_issue: dict[int, list[_Execution]] = {}
    for execution in executions:
        by_issue.setdefault(execution.run.number, []).append(execution)
    # A close journaled again (its run record failed to persist) is the same interval: keep the last.
    intervals: dict[tuple, dict[str, Any]] = {}
    for index, event in enumerate(_jsonl(store.root / "orch-events.jsonl")):
        if event.get("kind") == "phase":
            opened = event.get("phase_started_at")
            intervals[(event.get("issue"), event.get("phase"), opened) if opened else index] = event
    for event in intervals.values():
        if event.get("kind") != "phase" or type(event.get("issue")) is not int:
            continue
        at, seconds = _time(event.get("at")), _seconds(event.get("seconds"))
        if at is None or seconds is None or not isinstance(event.get("phase"), str):
            continue
        owners = [item for item in by_issue.get(event["issue"], []) if item.started <= at]
        if owners:
            owners[-1].events.append(event)
    return executions


def _row(execution: _Execution, now: datetime) -> dict[str, Any]:
    run = execution.run
    phases = [(event["phase"], float(event["seconds"]), bool(event.get("repeat_of"))) for event in execution.events]
    if run.status == "running" and run.phase and (opened := _time(run.phase_started_at)) is not None:
        # The journal is written before the run record: time an event already closed is not open again.
        closed = [at for event in execution.events if (at := _time(event.get("at"))) is not None]
        opened = max([opened, *closed])
        # The open segment repeats work by the same rule its closing event will use.
        work = work_event(run)
        phases.append((run.phase, max(0.0, (now - opened).total_seconds()), bool(work and work[1])))
    if run.status == "running":
        runtime = max(0.0, (now - execution.started).total_seconds())
    elif execution.finished is not None:
        runtime = max(0.0, (execution.finished - execution.started).total_seconds())
    else:
        runtime = None  # Recorded before finish times were kept.
    split: dict[str, float | None] = dict.fromkeys(PHASE_FIELDS, None)
    waste = None
    if phases:
        split = dict.fromkeys(PHASE_FIELDS, 0.0)
        for phase, seconds, _ in phases:
            if phase in PHASES:
                split[PHASES[phase]] += seconds
        # A wait inside repeated work occupies one interval: count it once.
        waste = sum(seconds for phase, seconds, repeated in phases if repeated or phase.endswith("-wait"))
    checking = split["checking_seconds"]
    return {
        "issue": run.number, "started_at": run.started_at, "status": run.status, "outcome": run.outcome,
        "lane": run.lane, "detail": run.detail, "phase": run.phase if run.status == "running" else None,
        "waited_seconds": run.waited_seconds if run.waited_seconds >= 0 else 0,
        **split,
        "runtime_seconds": runtime,
        "waste_seconds": waste,
        "waste_share": waste / runtime if waste is not None and runtime else None,
        "test_phase_share": checking / runtime if checking is not None and runtime else None,
        "related_tests": list(run.related_tests),
    }


def _history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [row for row in rows if row["status"] == "finished"]
    medians = {}
    for key in (*PHASE_FIELDS, "runtime_seconds"):
        values = [row[key] for row in finished if row[key] is not None]
        medians[key] = median(values) if values else None
    return {"runs": len(finished), "medians": medians}


def _comparisons(row: dict[str, Any], medians: dict[str, Any]) -> dict[str, Any]:
    """Arithmetic only: a running execution's phases beside recent medians."""
    if row["status"] != "running":
        return {}
    result = {}
    for key, middle in medians.items():
        if row[key] is None or middle is None:
            continue
        result[key] = {"seconds": row[key], "median_seconds": middle,
                       "ratio": row[key] / middle if middle else None, "above_median": row[key] > middle}
    return result


def _slowest_files(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime = sum(row["runtime_seconds"] or 0 for row in rows)
    seconds: dict[str, float] = {}
    selected: dict[str, int] = {}
    for row in rows:
        files: set[str] = set()
        for check in row["related_tests"]:
            files.update(check.get("selected_files") or [])
            for path, value in (check.get("file_seconds") or {}).items():
                if (value := _seconds(value)) is not None:
                    seconds[path] = seconds.get(path, 0) + value
                    files.add(path)
        for path in files:
            selected[path] = selected.get(path, 0) + 1
    ranked = sorted(selected, key=lambda path: (-seconds.get(path, 0), path))
    return [{"file": path, "seconds": seconds.get(path, 0), "runs": selected[path],
             "runtime_share": seconds.get(path, 0) / runtime if runtime else None} for path in ranked]


@dataclass
class _Waiting:
    number: int
    lane: str
    labels: set[str]
    parsed: ParsedIssue

    @property
    def startable(self) -> bool:
        """What the gate's start loop treats as eligible: ready, not blocked or awaiting a person."""
        return READY in self.labels and not self.labels & {BLOCKED, NEEDS_HUMAN}


def _waiting_issues(store: StateStore, config: Config, running: list[Run]) -> list[_Waiting]:
    """Open approved issues not running now, with their approved snapshot."""
    active = {run.number for run in running}
    waiting = []
    for issue in store.local_issues():
        labels = set(issue.get("labels") or [])
        if (issue.get("state") != "open" or APPROVED not in labels or RUNNING in labels
                or issue["number"] in active):
            continue
        approval = store.load_approval(issue["number"])
        if approval is None:
            continue
        # The tick's lane rule (lane labels against configured lanes), not a stored copy.
        waiting.append(_Waiting(issue["number"], issue_lane(labels, config.lanes), labels, parse_body(approval.body)))
    return waiting


def _hot_files(waiting: list[_Waiting]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in waiting:
        for path in dict.fromkeys(item.parsed.files):
            counts[path] = counts.get(path, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def _failure_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Recorded details of recent executions that did not merge, grouped as written."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["status"] != "finished" or row["outcome"] == "merged":
            continue
        if detail := row["detail"].strip():
            counts[detail] = counts.get(detail, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def _merges_since_tidy(store: StateStore) -> int:
    """Merges after the one tidy boundary: the latest tidy moment, journaled or in tidy.json.

    `fa tidy done` writes both with one timestamp. Without any tidy, every merge counts.
    """
    moments = [moment for event in _jsonl(store.root / "orch-events.jsonl")
               if event.get("kind") == "tidy-done" and (moment := _time(event.get("at"))) is not None]
    moments.append(_time(store.tidy_state().get("last_tidy_at")))
    latest = max((moment for moment in moments if moment is not None), default=None)
    merged, finished = store.merge_times(), store.finish_times()
    count = 0
    for run in store.read_history():
        if run.outcome != "merged":
            continue
        key, started = (run.number, run.started_at), _time(run.started_at)
        moment = _time(merged.get(key))
        if moment is None or (started is not None and moment < started):
            # No own merge time recorded: the merge happened at or before the finish.
            moment = _time(finished.get(key))
        if moment is None:
            # A legacy record with neither time: a start after the boundary still proves the
            # merge came after it; a start before it proves nothing, so it is not counted.
            moment = started
        if latest is None or (moment is not None and moment > latest):
            count += 1
    return count


def _test_slots(config: Config, running: list[Run], rows: list[dict[str, Any]], machine) -> dict[str, Any]:
    """The shared slot value (bounded by this project's test_slots), queue and waits."""
    state = machine["test_slots"]
    return {"current": max(1, min(state["slots"], config.test_slots) if state else config.test_slots),
            "queue_length": sum(run.phase == "test-slot-wait" for run in running),
            "total_wait_seconds": sum(row["test_slot_wait_seconds"] or 0 for row in rows)}


# plan.json may carry a throughput goal; facts copy only its plan keys (orch_plan.plan_fields) and never show a goal.

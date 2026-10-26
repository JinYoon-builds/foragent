"""`fa orch facts`: measured facts from local records only (D82).

Every number here is read or computed from files the gate and runners already
wrote: run records and their completion history, the phase journal, the shared
capacity files, recorded plan usage and approved issue snapshots. Nothing is
written, no process is started or signalled and GitHub is never called. The
facts name what binds now; they decide nothing and carry no throughput goal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from statistics import median
from typing import Any

from types import SimpleNamespace

from foragent import capacity, machine_budget, orch_queue, tidy
from foragent.config import Config, lane_executors
from foragent.issue import ParsedIssue, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, lane_order
from foragent.processes import read_process_identity
from foragent.state import Run, StateStore

RECENT = timedelta(days=7)
PHASES = {"implementing": "implementing_seconds", "checking": "checking_seconds",
          "test-slot-wait": "test_slot_wait_seconds", "baseline": "baseline_seconds"}
PHASE_FIELDS = tuple(PHASES.values())
HUMAN_RUNS = 6
HUMAN_ITEMS = 4


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _jsonl(path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _seconds(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and value >= 0 else None


@dataclass
class _Execution:
    run: Run
    started: datetime
    finished: datetime | None
    events: list[dict[str, Any]] = field(default_factory=list)


def _executions(store: StateStore, now: datetime) -> list[_Execution]:
    """Each execution once: completion history first, then current records not finished yet."""
    finished_at = store.finish_times()
    runs: dict[tuple[int, str], Run] = {(run.number, run.started_at): run for run in store.read_history()}
    for path in sorted((store.root / "runs").glob("*.json")):
        if path.stem.isdigit() and (run := store.get_run(int(path.stem))) is not None:
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
    for event in _jsonl(store.root / "orch-events.jsonl"):
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
        phases.append((run.phase, max(0.0, (now - opened).total_seconds()), False))
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


def _waiting_issues(store: StateStore, config: Config) -> list[_Waiting]:
    """Open approved issues not running now, with their approved snapshot."""
    active = {run.number for run in store.active_runs()}
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
    """Merges after the latest tidy: `fa tidy done` keeps a merge count, a tidy-done event a time."""
    events = [moment for event in _jsonl(store.root / "orch-events.jsonl")
              if event.get("kind") == "tidy-done" and (moment := _time(event.get("at"))) is not None]
    latest = max(events, default=None)
    recorded = _time(store.tidy_state().get("last_tidy_at"))
    if latest is None or (recorded is not None and recorded >= latest):
        return tidy.status(store)["merges_since"]  # The same count the tidy schedule uses.
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
        if moment is not None and moment > latest:
            count += 1
    return count


def _test_slots(config: Config, store: StateStore, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The shared slot value (bounded by this project's test_slots), queue and waits."""
    state = capacity.read_test_slots()
    return {"current": max(1, min(state["slots"], config.test_slots) if state else config.test_slots),
            "queue_length": sum(run.phase == "test-slot-wait" for run in store.active_runs()),
            "total_wait_seconds": sum(row["test_slot_wait_seconds"] or 0 for row in rows)}


def _quota(store: StateStore, now: datetime) -> dict[str, dict[str, Any]]:
    """Per executor, its tightest recorded window and the share likely unused at its reset."""
    history = _jsonl(store.root / "usage-history.jsonl")
    result = {}
    for executor, record in sorted(store.usage().items()):
        choice = None
        for window in (record or {}).get("windows") or []:
            used = _seconds(window.get("used_percent"))
            if used is None:
                continue
            reset = _time(window.get("resets_at"))
            remaining = 1.0 if reset is not None and reset <= now else max(0.0, 1 - used / 100)
            if choice is None or remaining < choice[0]:
                choice = (remaining, used, window, reset)
        if choice is None:
            continue
        remaining, used, window, reset = choice
        expiring = None
        seen = _time(record.get("seen_at"))
        if reset is not None and reset > now and seen is not None:
            # The observed rate within this same window, continued until its reset.
            earlier = []
            for row in history:
                if row.get("executor") != executor or (at := _time(row.get("seen_at"))) is None or at >= seen:
                    continue
                for item in row.get("windows") or []:
                    if (item.get("name") == window.get("name") and item.get("resets_at") == window.get("resets_at")
                            and (value := _seconds(item.get("used_percent"))) is not None):
                        earlier.append((at, value))
            if earlier:
                first, first_used = min(earlier)
                rate = max(0.0, (used - first_used) / (seen - first).total_seconds())
                projected = rate * max(0.0, (reset - seen).total_seconds()) / 100
                expiring = max(0.0, remaining - projected)
        result[executor] = {"window": window.get("name"), "remaining_share": remaining,
                            "expiring_unused_share": expiring, "resets_at": window.get("resets_at"),
                            "paused": store.executor_paused(executor, now)}
    # A usage-limit pause holds its lanes even when no usage window was recorded.
    try:
        pauses = json.loads((store.root / "executor-pauses.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pauses = {}
    for executor in sorted(pauses if isinstance(pauses, dict) else {}):
        if executor not in result and (until := store.executor_pause_until(executor, now)) is not None:
            result[executor] = {"window": None, "remaining_share": None, "expiring_unused_share": None,
                                "resets_at": until.isoformat(), "paused": True}
    return result


def _alive(run: Run, now: datetime) -> bool:
    """A sibling run holds a machine slot while its process lives; read without any signal."""
    if run.pid is None:
        return capacity._within_grace(run.started_at, now)
    try:
        return read_process_identity(run.pid) is not None
    except OSError:
        return True  # Unreadable is not proof that the slot is free.


def _machine_runs(store: StateStore, now: datetime) -> int:
    """capacity.machine_runs without its signal probe: this store's runs and live sibling runs."""
    count = capacity._store_slots(store, now, own=True)
    try:
        siblings = [root for root in store.root.parent.iterdir()
                    if root != store.root and ((root / "runs").is_dir() or (root / "run-starts").is_dir())]
    except OSError:
        siblings = []
    for root in siblings:
        try:
            sibling = StateStore(root, now=lambda: now)
            runs = sibling.active_runs()
            running = {run.number for run in runs}
            count += sum(_alive(run, now) for run in runs) + sum(
                1 for number, prepared in sibling.run_starts().items()
                if number not in running and capacity._within_grace(prepared, now))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return count


def _admission(store: StateStore, config: Config, root, now: datetime, waiting: list[_Waiting]) -> dict[str, Any]:
    """Replay the gate's start loop read-only: machine budget, project ceiling, lane holds, file rule.

    Each startable issue gets the first reason the loop would hold it for (cap,
    quota, structure), or starts and claims its Files for the issues after it.
    """
    from foragent.overlap_files import Claim, admits, any_overlap, claims, recorded_union
    from foragent.scheduler_start import _lane_held

    ctx = SimpleNamespace(store=store, config=config, repo_root=root)
    state = capacity.read_state(store)
    machine_cap = int(state["parallel"]) if state else capacity.INITIAL_PARALLEL
    if (ceiling := machine_budget.values()["max_parallel"]) is not None:
        machine_cap = min(machine_cap, ceiling)
    machine_active, project_active = _machine_runs(store, now), len(store.active_runs())
    free = max(0, machine_cap - machine_active)
    if config.max_parallel is not None:
        free = min(free, max(0, config.max_parallel - project_active))
    known, unknown = claims(ctx, measured=False)
    running = [path for claim in known for path in [*claim.files, *claim.parsed.exclusive]]
    contended = [item.number for item in waiting if item.startable and any_overlap(
        ctx, [*item.parsed.files, *item.parsed.exclusive], running)]
    held: dict[int, str] = {}
    remaining = free
    # The gate's merge=union answers, recorded when it last checked them (unchecked paths keep limits).
    union = recorded_union(store)
    items = {item.number: item for item in waiting if item.startable}
    lanes: dict[str, list[int]] = {}
    for item in items.values():
        lanes.setdefault(item.lane, []).append(item.number)
    # Same order as the tick: lanes by lane_order, each lane's local queue first.
    for lane in sorted(lanes, key=lane_order):
        order = orch_queue.ordered(store, lane, lanes[lane])
        if remaining > 0 and _lane_held(ctx, lane, now):
            held.update(dict.fromkeys(order, "quota"))
            continue
        for number in order:
            item = items[number]
            if remaining <= 0:
                held[number] = "cap"
            elif not admits(ctx, lane, item.parsed, known, unknown, union=union):
                held[number] = "structure"
            else:
                remaining -= 1
                run = Run(number, "", "", None, now.isoformat(), now.isoformat(), lane=lane)
                if item.parsed.files:
                    known.append(Claim(run, item.parsed, item.parsed.files))
                else:
                    unknown.append(run)
    return {"machine_cap": machine_cap, "machine_runs": machine_active, "project_runs": project_active,
            "project_cap": config.max_parallel, "free_run_slots": free,
            "waiting_issues": [item.number for item in waiting if item.startable],
            "overlap_blocked": contended, "held": {str(number): reason for number, reason in sorted(held.items())}}


def _resources(store: StateStore, config: Config, root, now: datetime, waiting, slots) -> dict[str, Any]:
    with capacity.observing():
        sample = capacity.machine_load()
    pressure, load = sample[0], float(sample[1])
    memory_headroom = pressure == "normal"
    headroom = memory_headroom and load < capacity.HEADROOM_LOAD
    work = _admission(store, config, root, now, waiting)
    held = set(work["held"].values())
    work["idle_while_waiting"] = work["free_run_slots"] > 0 and "structure" in held
    # Executors the waiting work needs: lanes of startable waiting issues and of runs queued for a slot.
    lanes = {item.lane for item in waiting if item.startable}
    lanes |= {run.lane for run in store.active_runs() if run.phase == "test-slot-wait"}
    needed = set().union(*(lane_executors(config, lane) for lane in lanes)) if lanes else set()
    quota = _quota(store, now)
    for name, value in quota.items():
        value["idle_while_waiting"] = bool(
            name in needed and not value["paused"] and (value["remaining_share"] or 0) > 0)
    waiting_now = bool(held) or slots["queue_length"] > 0
    return {
        "machine": {"load_per_cpu": load, "memory_pressure": pressure, "memory_headroom": memory_headroom,
                    "headroom": headroom, "idle_while_waiting": headroom and waiting_now},
        "quota": quota,
        # `admitted` is what `hold_test_slot` grants now, including machine.yml's default ceiling.
        "test_slots": {**slots, "admitted": capacity.effective_test_slots(config.test_slots),
                       "queue_with_machine_headroom": slots["queue_length"] > 0 and headroom},
        "parallelizable_work": work,
    }


def _binding(resources: dict[str, Any]) -> dict[str, Any] | None:
    """The one resource holding work now; machine and quota are real limits, the rest are fixable."""
    machine, slots, work = resources["machine"], resources["test_slots"], resources["parallelizable_work"]
    held: dict[str, list[str]] = {}
    for number, reason in work["held"].items():
        held.setdefault(reason, []).append(f"#{number}")
    if not work["waiting_issues"] and slots["queue_length"] == 0:
        return None
    if not machine["headroom"]:
        return {"kind": "machine", "normal": True,
                "detail": f"load {machine['load_per_cpu']:.2f} per CPU, memory {machine['memory_pressure']}"}
    if "quota" in held:
        return {"kind": "quota", "normal": True,
                "detail": "usage-limit pause holds " + ", ".join(held["quota"])}
    if slots["queue_length"] > 0:
        return {"kind": "test_slots", "normal": False,
                "detail": f"{slots['queue_length']} waiting for {slots['admitted']} admitted test slot(s) with machine headroom"}
    if "cap" in held:
        return {"kind": "cap", "normal": False,
                "detail": "no free parallel slot for " + ", ".join(held["cap"])}
    if "structure" in held:
        return {"kind": "structure", "normal": False,
                "detail": "file overlap holds " + ", ".join(held["structure"])
                          + f" with {work['free_run_slots']} free parallel slot(s)"}
    return None


def collect(store: StateStore, config: Config, root) -> dict[str, Any]:
    """All facts as one JSON-ready mapping."""
    now = store._now()
    rows = [_row(execution, now) for execution in _executions(store, now)
            if execution.run.status == "running" or execution.started >= now - RECENT]
    history = _history(rows)
    for row in rows:
        row["comparisons"] = _comparisons(row, history["medians"])
    waiting = _waiting_issues(store, config)
    slots = _test_slots(config, store, rows)
    measured = [row for row in rows if row["waste_seconds"] is not None and row["runtime_seconds"] is not None]
    wasted, runtime = sum(row["waste_seconds"] for row in measured), sum(row["runtime_seconds"] for row in measured)
    resources = _resources(store, config, root, now, waiting, slots)
    return {
        "at": now.astimezone(UTC).isoformat(),
        "window_days": RECENT.days,
        "runs": rows,
        "history": history,
        "test_slots": slots,
        "slowest_test_files": _slowest_files(rows),
        "waste": {"seconds": wasted, "runtime_seconds": runtime, "share": wasted / runtime if runtime else None},
        "hot_files": _hot_files(waiting),
        "failure_reasons": _failure_reasons(rows),
        "merges_since_tidy": _merges_since_tidy(store),
        "resources": resources,
        "binding": _binding(resources),
    }


def _s(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f} s"


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value * 100:.1f}%"


def human(facts: dict[str, Any]) -> str:
    """Five short blocks: runs, test slots and resources, hot files, failures, merges since tidy."""
    rows = facts["runs"]
    medians = facts["history"]["medians"]
    runs = [f"Runs (last {facts['window_days']} days): {len(rows)}; waste {_pct(facts['waste']['share'])}; "
            f"median runtime {_s(medians['runtime_seconds'])}"]
    for row in rows[-HUMAN_RUNS:]:
        state = row["phase"] or row["outcome"] or row["status"]
        runs.append(f"  #{row['issue']} {state}: runtime {_s(row['runtime_seconds'])}, "
                    f"implementing {_s(row['implementing_seconds'])}, checking {_s(row['checking_seconds'])}, "
                    f"slot wait {_s(row['test_slot_wait_seconds'])}, baseline {_s(row['baseline_seconds'])}, "
                    f"waited {_s(row['waited_seconds'])}")
    if facts["slowest_test_files"]:
        top = facts["slowest_test_files"][0]
        runs.append(f"  Slowest test file: {top['file']} {_s(top['seconds'])} in {top['runs']} execution(s), "
                    f"{_pct(top['runtime_share'])} of runtime")
    slots, resources = facts["test_slots"], facts["resources"]
    machine, work = resources["machine"], resources["parallelizable_work"]
    capacity_lines = [
        f"Test slots: current {slots['current']} (admitted {resources['test_slots']['admitted']}), queue {slots['queue_length']}, "
        f"waited {_s(slots['total_wait_seconds'])}",
        f"  Machine: load {machine['load_per_cpu']:.2f} per CPU, memory {machine['memory_pressure']}",
        f"  Parallel: {work['free_run_slots']} free (machine {work['machine_runs']}/{work['machine_cap']}"
        + (f", project {work['project_runs']}/{work['project_cap']}" if work["project_cap"] is not None else "")
        + "); held: " + (", ".join(f"#{n} {reason}" for n, reason in work["held"].items()) or "none"),
    ]
    for name, quota in resources["quota"].items():
        unused = quota["expiring_unused_share"]
        capacity_lines.append(f"  Quota {name}: " + ("paused" if quota["paused"] else
                              f"{_pct(quota['remaining_share'])} left")
                              + (f", ~{_pct(unused)} unused at reset {quota['resets_at']}" if unused is not None else ""))
    binding = facts["binding"]
    capacity_lines.append("  Binding: " + ("none" if binding is None else
                          f"{binding['kind']} ({'normal' if binding['normal'] else 'fixable'}): {binding['detail']}"))
    hot = list(facts["hot_files"].items())
    hot_lines = [f"Hot files (approved, waiting): {'none' if not hot else len(hot)}"]
    hot_lines += [f"  {path}: {count}" for path, count in hot[:HUMAN_ITEMS]]
    reasons = list(facts["failure_reasons"].items())
    reason_lines = [f"Failure reasons (last {facts['window_days']} days): {'none' if not reasons else len(reasons)}"]
    reason_lines += [f"  {count}x {text[:100]}" for text, count in reasons[:HUMAN_ITEMS]]
    blocks = [runs, capacity_lines, hot_lines, reason_lines, [f"Merges since tidy: {facts['merges_since_tidy']}"]]
    return "\n\n".join("\n".join(block) for block in blocks)

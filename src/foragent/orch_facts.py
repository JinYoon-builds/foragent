"""`fa orch facts`: measured facts from local records only (D82).

Every number here is read or computed from files the gate and runners already
wrote: run records and their completion history, the phase journal, the shared
capacity files, recorded plan usage and approved issue snapshots. Nothing is
written, no process is started or signalled and GitHub is never called. The
facts name what binds now; they decide nothing and carry no throughput goal.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
from statistics import median
import tempfile
from types import SimpleNamespace
from typing import Any

from foragent import capacity, machine_budget, orch_queue
from foragent.config import Config, in_run_hours, lane_executors
from foragent.issue import ParsedIssue, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, lane_order
from foragent.processes import read_process_identity
from foragent.state import Run, StateStore
from foragent.state_runs import work_event

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


def _current_runs(store: StateStore) -> list[Run]:
    """Each issue's current run record, read once per facts snapshot."""
    runs = []
    for path in sorted((store.root / "runs").glob("*.json")):
        if path.stem.isdigit() and (run := store.get_run(int(path.stem))) is not None:
            runs.append(run)
    return runs


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


def _probe(pid: int) -> bool:
    """Whether a process lives, read without any signal; unreadable is not proof it ended."""
    try:
        return read_process_identity(pid) is not None
    except OSError:
        return True


def _alive(run: Run, now: datetime, alive: dict[int, bool]) -> bool:
    """A sibling run holds a machine slot while its process lives (probed during the frozen read)."""
    if run.pid is None:
        return capacity._within_grace(run.started_at, now)
    return alive.get(run.pid, True)


def _machine_runs(store: StateStore, now: datetime, running: list[Run], alive: dict[int, bool]) -> int:
    """capacity.machine_runs without its signal probe: this store's runs and live sibling runs."""
    numbers = {run.number for run in running}
    count = len(running) + sum(1 for number, prepared in store.run_starts().items()
                               if number not in numbers and capacity._within_grace(prepared, now))
    try:
        siblings = [root for root in store.root.parent.iterdir()
                    if root != store.root and ((root / "runs").is_dir() or (root / "run-starts").is_dir())]
    except OSError:
        siblings = []
    for root in siblings:
        try:
            sibling = StateStore(root, now=lambda: now)
            runs = _still_running(sibling, sibling.active_runs())
            running = {run.number for run in runs}
            count += sum(_alive(run, now, alive) for run in runs) + sum(
                1 for number, prepared in sibling.run_starts().items()
                if number not in running and capacity._within_grace(prepared, now))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return count


def _admission(
    store: StateStore, config: Config, root, now: datetime, waiting: list[_Waiting], running: list[Run],
    hold_all: str | None = None, machine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the gate's start loop read-only: machine budget, project ceiling, lane holds, file rule.

    Each startable issue gets the first reason the loop would hold it for (cap,
    quota, structure), or starts and claims its Files for the issues after it.
    """
    from foragent.overlap_files import Claim, admits, any_overlap, claims, recorded_union
    from foragent.scheduler_start import _lane_held

    ctx = SimpleNamespace(store=store, config=config, repo_root=root)
    state = capacity.read_state(store)
    machine_cap = int(state["parallel"]) if state else capacity.INITIAL_PARALLEL
    if (ceiling := machine["budget"]["max_parallel"]) is not None:
        machine_cap = min(machine_cap, ceiling)
    machine_active, project_active = _machine_runs(store, now, running, machine["alive"]), len(running)
    free = max(0, machine_cap - machine_active)
    if config.max_parallel is not None:
        free = min(free, max(0, config.max_parallel - project_active))
    # The same running set as every other fact: a half-written finish claims nothing.
    live = {(run.number, run.started_at) for run in running}
    known, unknown = claims(ctx, measured=False)
    known = [claim for claim in known if (claim.run.number, claim.run.started_at) in live]
    unknown = [run for run in unknown if (run.number, run.started_at) in live]
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
    ordered = [(lane, orch_queue.ordered(store, lane, lanes[lane])) for lane in sorted(lanes, key=lane_order)]
    if hold_all in ("github", "run_hours"):
        # The tick skips the whole start loop before any cap, quota or file check.
        held.update({number: hold_all for _, order in ordered for number in order})
        ordered = []
    stopped = False
    for lane, order in ordered:
        if stopped:
            held.update(dict.fromkeys(order, "machine"))
            continue
        if remaining > 0 and _lane_held(ctx, lane, now):
            held.update(dict.fromkeys(order, "quota"))
            continue
        for number in order:
            item = items[number]
            if stopped:
                held[number] = "machine"
            elif remaining <= 0:
                held[number] = "cap"
            elif not admits(ctx, lane, item.parsed, known, unknown, union=union):
                held[number] = "structure"
            elif hold_all == "machine":
                # Passed every start check; critical memory ends the tick here, so nothing after it starts.
                held[number] = "machine"
                stopped = True
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


def _resources(store: StateStore, config: Config, root, now: datetime, waiting, slots, running,
               machine: dict[str, Any]) -> dict[str, Any]:
    sample = machine["sample"]
    pressure, load = sample[0], float(sample[1])
    # The gate's rules: only critical memory holds starts and test slots (#497); load at or over
    # OVERLOAD_LOAD shrinks the run and slot budgets; below HEADROOM_LOAD they may grow.
    memory_headroom = pressure != "critical"
    headroom = memory_headroom and load < capacity.HEADROOM_LOAD
    overloaded = not memory_headroom or load >= capacity.OVERLOAD_LOAD
    # Recorded holds in the tick's order: a GitHub wait skips the tick before starting,
    # then run hours, then critical memory inside the start loop.
    github_until = store.github_wait_until(now)
    hold_all = ("github" if github_until is not None
                else "run_hours" if not in_run_hours(now.astimezone(), config.run_hours)
                else "machine" if not memory_headroom else None)
    work = _admission(store, config, root, now, waiting, running, hold_all, machine)
    work["github_wait_until"] = github_until.isoformat() if github_until is not None else None
    # Running work held in flight: a run waiting for files other runs hold.
    work["in_flight"] = {str(run.number): "structure" for run in sorted(running, key=lambda run: run.number)
                         if run.phase == "overlap-wait"}
    held = set(work["held"].values()) | set(work["in_flight"].values())
    work["idle_while_waiting"] = work["free_run_slots"] > 0 and "structure" in held
    # Executors the waiting work needs: startable waiting issues and runs waiting in flight.
    lanes = {item.lane for item in waiting if item.startable}
    lanes |= {run.lane for run in running if run.phase in ("test-slot-wait", "overlap-wait")}
    needed = set().union(*(lane_executors(config, lane) for lane in lanes)) if lanes else set()
    quota = _quota(store, now)
    for name, value in quota.items():
        value["idle_while_waiting"] = bool(
            name in needed and not value["paused"] and (value["remaining_share"] or 0) > 0)
    waiting_now = bool(held) or slots["queue_length"] > 0
    return {
        "machine": {"load_per_cpu": load, "memory_pressure": pressure, "memory_headroom": memory_headroom,
                    "headroom": headroom, "overloaded": overloaded,
                    "idle_while_waiting": headroom and waiting_now},
        "quota": quota,
        # `admitted` is what `hold_test_slot` grants now, including machine.yml's default ceiling.
        "test_slots": {**slots, "admitted": capacity.effective_test_slots(
                           config.test_slots, state=machine["test_slots"], machine=machine["budget"]),
                       "queue_with_machine_headroom": slots["queue_length"] > 0 and headroom},
        "parallelizable_work": work,
    }


def _binding(resources: dict[str, Any]) -> dict[str, Any] | None:
    """The one resource holding work now; machine and quota are real limits, the rest are fixable."""
    machine, slots, work = resources["machine"], resources["test_slots"], resources["parallelizable_work"]
    held: dict[str, list[str]] = {}
    for number, reason in work["held"].items():
        held.setdefault(reason, []).append(f"#{number}")
    for number, reason in work["in_flight"].items():
        held.setdefault(reason, []).append(f"#{number} (running)")
    if not work["waiting_issues"] and slots["queue_length"] == 0 and not work["in_flight"]:
        return None
    if machine["overloaded"]:
        # Critical memory holds every start; overload shrinks the shared budgets this tick.
        return {"kind": "machine", "normal": True,
                "detail": f"load {machine['load_per_cpu']:.2f} per CPU, memory {machine['memory_pressure']}"}
    if "github" in held:
        return {"kind": "github", "normal": True,
                "detail": f"GitHub rate limit until {work['github_wait_until']}; the tick starts nothing: "
                          + ", ".join(held["github"])}
    if "run_hours" in held:
        return {"kind": "run_hours", "normal": True,
                "detail": "outside run_hours; the gate starts nothing now: " + ", ".join(held["run_hours"])}
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


# Never copied or opened: plan.json may carry a throughput goal, which facts must not show.
NOT_READ = {"plan.json"}
FREEZE_ATTEMPTS = 3


def _lock_shared(stack: ExitStack, path: Path) -> bool:
    """Hold a shared flock on an existing lock file without creating or changing it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    stack.callback(os.close, descriptor)
    fcntl.flock(descriptor, fcntl.LOCK_SH)
    stack.callback(fcntl.flock, descriptor, fcntl.LOCK_UN)
    return True


def _copy(source: Path, target: Path) -> None:
    try:
        data = source.read_bytes()
    except FileNotFoundError:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


GLOBAL_CONFIG = "global-config.yml"


def _freeze(store: StateStore, into: Path, checkout: Path | None = None) -> tuple[StateStore, dict[str, Any]] | None:
    """Read every input once under the writers' locks; None when a lock appeared meanwhile.

    Lock order: every repository's state lock (sorted), the machine run-budget lock,
    then the sensor lock. Writers hold at most one of them at a time, so a shared
    hold of all cannot deadlock. A lock file that does not exist yet has never been
    written under; if one appears during the read, the caller reads again.
    """
    parent = store.root.parent

    def discover() -> list[Path]:
        try:
            found = {path for path in parent.iterdir() if path.is_dir() and (
                (path / "runs").is_dir() or (path / "run-starts").is_dir())}
        except OSError:
            found = set()
        return sorted(found | {store.root})

    def lock_set(stores: list[Path]) -> list[Path]:
        return [*(path / "state.lock" for path in stores), parent / capacity.LOCK_FILE,
                capacity.machine_root() / capacity.SENSOR_LOCK]

    stores = discover()
    locks = lock_set(stores)
    with ExitStack() as stack:
        held = [_lock_shared(stack, path) for path in locks]
        # Start reservations (and a sibling's first one, which creates its store) happen under
        # the machine lock held now: discover again, so none made before it is missed.
        if discover() != stores:
            return None
        for source in stores:
            target = into / "data" / source.name
            names = [*source.glob("*.json"), *source.glob("*.jsonl")] if source == store.root else [
                source / "runs.jsonl"]
            for path in names:
                if path.name not in NOT_READ:
                    _copy(path, target / path.name)
            for directory in ("runs", "run-starts", *(("approvals",) if source == store.root else ())):
                for path in (source / directory).glob("*.json"):
                    _copy(path, target / directory / path.name)
        _copy(parent / capacity.STATE_FILE, into / "data" / capacity.STATE_FILE)
        # Config files: the global config and the checkout's .foragent.yml, read once like the records.
        _copy(Path.home() / ".config/foragent/config.yml", into / GLOBAL_CONFIG)
        if checkout is not None:
            _copy(checkout / ".foragent.yml", into / "checkout" / ".foragent.yml")
        with capacity.observing():
            sample = capacity.machine_load()
        # Sibling processes are probed now, while their records cannot change.
        alive = {}
        for path in (into / "data").glob("*/runs/*.json"):
            if path.parent.parent.name == store.root.name:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if record.get("status") == "running" and type(record.get("pid")) is int:
                alive[record["pid"]] = _probe(record["pid"])
        machine = {"test_slots": capacity.read_test_slots(), "budget": machine_budget.values(), "sample": sample,
                   "now": store._now(), "alive": alive}
        # A lock file created meanwhile (a first write under it) means the read was not covered.
        if discover() != stores or [path.exists() for path in lock_set(stores)] != held:
            return None
    (into / "data" / store.root.name).mkdir(parents=True, exist_ok=True)
    observed = machine["now"]
    return StateStore(into / "data" / store.root.name, now=lambda: observed), machine


def collect(store: StateStore, config: Config, root, *, checkout: bool = False) -> dict[str, Any]:
    """All facts as one JSON-ready mapping, computed from one frozen read of every input.

    ``config`` is the checkout config. With ``checkout``, it is loaded again from the
    frozen copies of ``root/.foragent.yml`` and the global config (``config`` stays the
    fallback). The gate's committed config recorded by the last tick applies over it,
    merged with the same frozen global config.
    """
    from foragent.config import ConfigError, load_config
    from foragent.scheduler_config import tick_config

    for attempt in range(FREEZE_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="foragent-facts-") as directory:
            frozen = _freeze(store, Path(directory), Path(root) if checkout else None)
            if frozen is None and attempt < FREEZE_ATTEMPTS - 1:
                continue
            if frozen is None:
                raise OSError("State locks kept appearing while facts were read; try again")
            snapshot, machine = frozen
            global_path = Path(directory) / GLOBAL_CONFIG
            if checkout:
                try:
                    config = load_config(Path(directory) / "checkout", global_path=global_path)
                except (ConfigError, OSError, ValueError, TypeError):
                    pass  # The checkout file vanished or broke meanwhile: keep the command's config.
            return _collect(snapshot, tick_config(snapshot, config, global_path), root, machine)
    raise AssertionError("unreachable")


def _still_running(store: StateStore, runs: list[Run]) -> list[Run]:
    """Running records whose execution has no completion entry yet.

    finish_run appends the history entry before it replaces the run record; a record
    read between the two is finished, not running.
    """
    finished = {(run.number, run.started_at) for run in store.read_history()}
    return [run for run in runs if run.status == "running" and (run.number, run.started_at) not in finished]


def _collect(store: StateStore, config: Config, root, machine: dict[str, Any]) -> dict[str, Any]:
    now = machine["now"]  # The moment the inputs were read.
    current = _current_runs(store)
    running = _still_running(store, current)
    rows = [_row(execution, now) for execution in _executions(store, now, current)
            if execution.run.status == "running" or execution.started >= now - RECENT]
    history = _history(rows)
    for row in rows:
        row["comparisons"] = _comparisons(row, history["medians"])
    waiting = _waiting_issues(store, config, running)
    slots = _test_slots(config, running, rows, machine)
    measured = [row for row in rows if row["waste_seconds"] is not None and row["runtime_seconds"] is not None]
    wasted, runtime = sum(row["waste_seconds"] for row in measured), sum(row["runtime_seconds"] for row in measured)
    resources = _resources(store, config, root, now, waiting, slots, running, machine)
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

"""Resource, admission and binding facts from the frozen read, by the gate's own start rules."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from foragent import capacity, orch_facts_machine, orch_queue
from foragent.config import Config, in_run_hours, lane_executors
from foragent.processes import read_process_identity
from foragent.state import Run, StateStore

if TYPE_CHECKING:
    from foragent.orch_facts_runs import _Waiting


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _jsonl(path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        # A missing journal or a non-directory ancestor both mean no records exist.
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


def _still_running(store: StateStore, runs: list[Run]) -> list[Run]:
    """Running records whose execution has no completion entry yet.

    finish_run appends the history entry before it replaces the run record; a record
    read between the two is finished, not running.
    """
    finished = {(run.number, run.started_at) for run in store.read_history()}
    return [run for run in runs if run.status == "running" and (run.number, run.started_at) not in finished]


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
    count = sum(capacity.phase_holds_slot(run.phase) for run in running)
    count += sum(1 for number, prepared in store.run_starts().items()
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
            count += sum(capacity.phase_holds_slot(run.phase) and _alive(run, now, alive) for run in runs) + sum(
                1 for number, prepared in sibling.run_starts().items()
                if number not in running and capacity._within_grace(prepared, now))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return count


def _admission(
    store: StateStore, config: Config, root, now: datetime, waiting: list[_Waiting], running: list[Run],
    hold_all: str | None = None, machine: dict[str, Any] | None = None, *, probe: bool = True,
) -> dict[str, Any]:
    """Replay the gate's start loop read-only: machine budget, project ceiling, lane holds, file rule.

    Each startable issue gets the first reason the loop would hold it for (cap,
    quota, structure), or starts and claims its Files for the issues after it.
    """
    from foragent.overlap_files import Claim, admits, any_overlap, claims, recorded_union
    from foragent.scheduler_start import _lane_held

    ctx = SimpleNamespace(store=store, config=config, repo_root=root, now=lambda: now)
    if not probe:
        ctx.case_sensitive = None  # Unknown: intersects() then compares paths case-sensitively.
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
    # The tick's own start order (scheduler_start._ranked): fewest files overlapping running
    # claims, the global priority list, then each lane's queue order. Ranked once, before starts.
    from foragent.scheduler_start import _ranked

    ready = {lane: orch_queue.ordered(store, lane, numbers) for lane, numbers in lanes.items()}
    ranked = _ranked(ctx, ready, claims=list(known))
    if hold_all in ("github", "run_hours"):
        # The tick skips the whole start loop before any cap, quota or file check.
        held.update({number: hold_all for _, number in ranked})
        ranked = []
    for index, (lane, number) in enumerate(ranked):
        item = items[number]
        if remaining <= 0:
            # No slot ends the loop: nothing after this point is checked.
            held.update({later: "cap" for _, later in ranked[index:]})
            break
        if _lane_held(ctx, lane, now, number):  # The issue's own quota decision, as the tick makes it.
            held[number] = "quota"
        elif not admits(ctx, lane, item.parsed, known, unknown, union=union):
            held[number] = "structure"
        elif hold_all == "machine":
            # Passed every start check; critical memory ends the tick, so nothing after it starts.
            held.update({later: "machine" for _, later in ranked[index:]})
            break
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
               machine: dict[str, Any], *, probe: bool = True) -> dict[str, Any]:
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
    work = _admission(store, config, root, now, waiting, running, hold_all, machine, probe=probe)
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
        "machine": {**orch_facts_machine.sample(machine, now), "load_per_cpu": load, "memory_pressure": pressure,
                    "memory_headroom": memory_headroom, "headroom": headroom, "overloaded": overloaded,
                    "idle_while_waiting": headroom and waiting_now}, **orch_facts_machine.local(store, config, now, running, machine),
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

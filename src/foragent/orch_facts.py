"""`fa orch facts`: measured facts from local records only (D82).

Every number here is read or computed from files the gate and runners already
wrote: run records and their completion history, the phase journal, the shared
capacity files, recorded plan usage and approved issue snapshots. Nothing is
changed apart from lock ownership metadata, no process is started or signalled
on a successful read and GitHub is never called. The facts name what binds now;
they decide nothing and carry no throughput goal.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from foragent import capacity, capacity_sessions, machine_budget, orch_facts_baselines, orch_facts_ci, orch_facts_decided, orch_facts_kinds, orch_facts_machine, orch_facts_reviews, orch_facts_stall, orch_plan, orch_role_schedule, resource_sample, step_weights
from foragent.config import Config
from foragent.orch_facts_resources import _binding, _probe, _resources, _still_running
from foragent.orch_facts_runs import (
    RECENT, _comparisons, cross_lane_orders, _executions, _failure_reasons, _file_conflicts, _history, _hot_files, _merges_since_tidy, _row,
    _slowest_files, merged_keys, _test_slots, _waiting_issues,
)
from foragent.file_cache import temporary_reads
from foragent.file_locks import lock_order, locked, reporting
from foragent.state import PR_LINES, Run, StateStore
from foragent.state_logs import FLOW_LOCK
from foragent.tracker import local_issue_models

HUMAN_RUNS = 6
HUMAN_ITEMS = 4


def _current_runs(store: StateStore) -> list[Run]:
    """Each issue's current run record, read once per facts snapshot."""
    runs = []
    for path in sorted((store.root / "runs").glob("*.json")):
        if path.stem.isdigit() and (run := store.get_run(int(path.stem))) is not None:
            runs.append(run)
    return runs


FREEZE_ATTEMPTS = 3


def _lock_shared(stack: ExitStack, path: Path) -> bool:
    """Hold an existing lock shared; record this reader beside the unchanged lock inode."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    stack.callback(os.close, descriptor)
    stack.enter_context(locked(descriptor, path, mode=fcntl.LOCK_SH))
    return True


def _copy_plan(source: Path, target: Path) -> None:
    """Copy only the plan keys (lane queues and the rest the tick reads); never a goal field."""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(orch_plan.plan_fields(data)), encoding="utf-8")


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

    Lock order: machine run-budget lock, every repository's state lock, then the sensor lock,
    sorted by the guard's own key (file_locks.lock_order) so store names never disagree with it. Admission writers also take machine before state.
    A lock file that does not exist yet has never been
    written under; if one appears during the read, the caller reads again.
    """
    parent = store.root.parent

    def discover() -> list[Path]:
        try:
            found = {path for path in parent.iterdir() if path.is_dir() and capacity._holds_anything(path)}
        except OSError:
            found = set()
        # Stores under the machine root are read for the machine-wide test-slot queue and merge samples.
        return sorted(found | {store.root} | set(resource_sample.machine_stores(store))
                      | orch_facts_machine.merge_store_roots(store))

    def lock_set(stores: list[Path]) -> list[Path]:
        return sorted([parent / capacity.LOCK_FILE, *(path / "state.lock" for path in stores),
                       capacity.machine_root() / capacity.SENSOR_LOCK], key=lock_order)

    stores = discover()
    locks = lock_set(stores)
    with reporting(store.root), ExitStack() as stack:
        held = [_lock_shared(stack, path) for path in locks]
        # Start reservations (and a sibling's first one, which creates its store) happen under
        # the machine lock held now: discover again, so none made before it is missed.
        if discover() != stores:
            return None
        for source in stores:
            orch_facts_machine.freeze_slot_store(store, source, into, _copy)
            orch_facts_machine.freeze_merge_store(store, source, into, _copy)
            if source != store.root and source.parent != parent:
                continue
            target = into / "data" / source.name
            # flow.lock too: a store without it reads as one that never recorded a flow record.
            names = [*source.glob("*.json"), *source.glob("*.jsonl"), source / FLOW_LOCK] if source == store.root else [
                source / "runs.jsonl"]
            for path in names:
                (_copy_plan if path.name == "plan.json" else _copy)(path, target / path.name)
            for directory in ("runs", "run-starts", capacity_sessions.WORKLOADS_DIR,
                              *(("approvals", "inbox/requests") if source == store.root else ())):
                for path in (source / directory).glob("*.json"):
                    _copy(path, target / directory / path.name)
            if source == store.root:
                orch_facts_machine.copy_reviews(source, target)
                # The runner's latest line measurement per issue (#2731), read like its reviews.
                for path in (source / "runs").glob(f"issue-*/{PR_LINES}"):
                    _copy(path, target / "runs" / path.parent.name / path.name)
                orch_facts_reviews.freeze_costs(source, target, store._now() - RECENT)
        _copy(parent / capacity.STATE_FILE, into / "data" / capacity.STATE_FILE)
        # Config files: the global config and the checkout's .foragent.yml, read once like the records.
        _copy(Path.home() / ".config/foragent/config.yml", into / GLOBAL_CONFIG)
        if checkout is not None:
            _copy(checkout / ".foragent.yml", into / "checkout" / ".foragent.yml")
        stored, sample = orch_facts_machine.freeze_sample(into, _copy)
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
        if (into / "data").is_dir():
            alive.update(orch_facts_machine.probe_workloads(into / "data", _probe))
        machine = {"test_slots": capacity.read_test_slots(), "budget": machine_budget.values(), "sample": sample,
                   "step_weights": step_weights.learned(store),
                   "stored": stored, "slot_stores": into / orch_facts_machine.SLOT_STORES,
                   "merge_stores": into / orch_facts_machine.MERGE_STORES, "now": store._now(), "alive": alive}
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
        with tempfile.TemporaryDirectory(prefix="foragent-facts-") as directory, temporary_reads(Path(directory)):
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
            config = tick_config(snapshot, config, global_path)
            facts = _collect(snapshot, config, root, machine)
            # The machine-wide baseline cache, read in place as of the same moment.
            facts.update(orch_facts_baselines.collect(store.root.parent / "baselines", root, config.repo,
                                                      machine["now"], RECENT))
            return facts
    raise AssertionError("unreachable")


def _collect(store: StateStore, config: Config, root, machine: dict[str, Any]) -> dict[str, Any]:
    from foragent import scheduler

    now = machine["now"]  # The moment the inputs were read.
    current = _current_runs(store)
    running = _still_running(store, current)
    every = _executions(store, now, current)
    executions = [execution for execution in every
                  if execution.run.status == "running" or execution.started >= now - RECENT]
    rows = [_row(execution, now) for execution in executions]
    merged = merged_keys(store, rows)
    reviews = orch_facts_reviews.attach(store, executions, rows, merged)
    areas = orch_facts_reviews.attach_project_review(store, root, executions, rows, machine)
    history = _history(rows)
    for row in rows:
        row["comparisons"] = _comparisons(row, history["medians"])
    waiting = _waiting_issues(store, config, running)
    slots = _test_slots(config, running, rows, machine)
    measured = [row for row in rows if row["waste_seconds"] is not None and row["runtime_seconds"] is not None]
    wasted, runtime = sum(row["waste_seconds"] for row in measured), sum(row["runtime_seconds"] for row in measured)
    resources = _resources(store, config, root, now, waiting, slots, running, machine)
    supply = orch_facts_stall.supply(config, running, resources["parallelizable_work"])
    return {
        "at": now.astimezone(UTC).isoformat(),
        "window_days": RECENT.days,
        "runs": rows,
        "areas": areas,
        "history": history,
        "test_slots": slots,
        "slowest_test_files": _slowest_files(rows),
        "waste": {"seconds": wasted, "runtime_seconds": runtime, "share": wasted / runtime if runtime else None},
        "hot_files": _hot_files(waiting),
        "file_conflicts": _file_conflicts(rows, merged),
        "failure_reasons": _failure_reasons(rows, merged),
        "merges_since_tidy": _merges_since_tidy(store),
        "resources": resources,
        "step_weights": machine["step_weights"],
        "binding": _binding(resources),
        "role_schedule": orch_role_schedule.facts(store, config, now),
        # Finished orders' issues that changed another lane's paths: evidence for the architect only.
        "cross_lane_orders": cross_lane_orders(
            store, config, every, orch_role_schedule.finished_orders(local_issue_models(store)),
            scheduler._filesystem_case_sensitive(Path(root))),
        **orch_facts_decided.collect(store, now, RECENT),
        # Recent issue-attributed Claude dollar cost by merged state and review rounds (#2730).
        "claude_cost": orch_facts_reviews.claude_cost(store, every, rows, now, RECENT),
        # Each CI job's recent main and train durations beside its limit (#2497).
        "ci_jobs": orch_facts_ci.jobs(store),
        # Where the last CI read's search for train runs stopped, and its page bound (#2560).
        "ci_search": orch_facts_ci.search(store),
        # Free lanes and what admission would start in them; the tick's supply-low hold reads it.
        "supply": supply,
        **reviews, **orch_facts_kinds.collect(store, config, root, now, rows, running, waiting, supply, config.repo),
    }


def _s(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f} s"


def _n(value: int | None) -> str:
    return "?" if value is None else str(value)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value * 100:.1f}%"


def _waiting_text(facts: dict[str, Any]) -> str:
    """The waiting-on-a-person numbers, appended to the kinds line so the block keeps its lines."""
    waiting = facts.get("kind_numbers", {}).get("waiting_on_person")
    if not waiting:
        return ""
    hours = waiting["oldest_waiting_seconds"] / 3600
    return f"; waiting on a person: {waiting['waiting']}, oldest {waiting['oldest_issue']} ({hours:.1f} h)"


def _decided_lines(facts: dict[str, Any], language: str) -> list[str]:
    """Decided issues waiting for tests, and decision-to-approval time, as two lines."""
    from foragent.i18n import text

    waiting, approval = facts["tests_waiting"], facts["decision_to_approval"]
    issues = waiting["issues"] or []
    shown = (" (" + ", ".join(f"#{n}" for n in issues[:HUMAN_ITEMS]) + (", ..." if len(issues) > HUMAN_ITEMS else "")
             + ")") if issues else ""
    minutes = approval["median_minutes"]
    return [text("facts_tests_waiting", language, count=_n(waiting["count"]), issues=shown),
            text("facts_decision_to_approval", language, days=facts["window_days"],
                 median="?" if minutes is None else f"{minutes:.0f}", approved=approval["approved"],
                 unknown=approval["unknown"])]


def _ci_alarm_lines(facts: dict[str, Any], language: str) -> list[str]:
    """One line naming each alarmed CI job with its latest and limit minutes, or no line without alarms."""
    from foragent.i18n import text

    parts = []
    for job in facts.get("ci_jobs") or []:
        if not (job["near_limit"] or job["rising"]):
            continue
        key = ("facts_ci_near_limit" if job["near_limit"] else
               "facts_ci_rising" if job["limit_minutes"] is not None else "facts_ci_rising_unknown")
        parts.append(text(key, language, job=job["job"], latest=f"{job['latest_minutes']:g}",
                          limit=f"{job['limit_minutes']:g}" if job["limit_minutes"] is not None else ""))
    return [text("facts_ci_alarms", language, jobs="; ".join(parts))] if parts else []


def _shown_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The latest executions, running ones first in line for a place, in their recorded order."""
    running = [index for index, row in enumerate(rows) if row["status"] == "running"][-HUMAN_RUNS:]
    others = [index for index in range(len(rows)) if index not in running]
    keep = set(running) | set(others[max(0, len(others) - (HUMAN_RUNS - len(running))):] if len(running) < HUMAN_RUNS else ())
    return [row for index, row in enumerate(rows) if index in keep]


def human(facts: dict[str, Any], language: str = "en") -> str:
    """Five short blocks: runs with review rounds, test slots and resources, hot files, failures, merges since tidy."""
    rows = facts["runs"]
    medians = facts["history"]["medians"]
    runs = [f"Runs (last {facts['window_days']} days): {len(rows)}; waste {_pct(facts['waste']['share'])}; "
            f"median runtime {_s(medians['runtime_seconds'])}"]
    for row in _shown_runs(rows):
        state = row["phase"] or row["outcome"] or row["status"]
        runs.append(f"  #{row['issue']} {state}: runtime {_s(row['runtime_seconds'])}, "
                    f"implementing {_s(row['implementing_seconds'])}, checking {_s(row['checking_seconds'])}, "
                    f"slot wait {_s(row['test_slot_wait_seconds'])}, baseline {_s(row['baseline_seconds'])}, "
                    f"waited {_s(row['waited_seconds'])}, files declared {_n(row['declared_file_count'])} "
                    f"changed {_n(row['changed_file_count'])}, overlaps {len(row['overlap_records'])}, "
                    + orch_facts_reviews.loop_text(row, language))
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
        "  " + orch_facts_machine.human_line(resources, language),
        *("  " + line for line in orch_facts_machine.process_lines(resources, language)),
        step_weights.human(facts["step_weights"]),
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
    conflicts = list(facts["file_conflicts"].items())[:HUMAN_ITEMS]
    hot_lines.append(f"Overlap conflicts (merged, last {facts['window_days']} days): " + ("; ".join(
        f"{path} {item['conflicted_merges']}/{item['overlapping_merges']} ({_pct(item['conflict_rate'])}), "
        f"resolution {_s(item['resolution_seconds'])}, redo {item['redo_count']}" for path, item in conflicts) or "none"))
    reasons = list(facts["failure_reasons"].items())
    reason_lines = [f"Failure reasons (last {facts['window_days']} days): {'none' if not reasons else len(reasons)}"]
    reason_lines += [f"  {count}x {text[:100]}" for text, count in reasons[:HUMAN_ITEMS]]
    blocks = [runs + ["  " + line for line in [*orch_facts_reviews.human_lines(facts, language, HUMAN_ITEMS),
                                               *orch_facts_reviews.project_review_lines(facts, language, HUMAN_ITEMS)]], capacity_lines, hot_lines, reason_lines, [f"Merges since tidy: {facts['merges_since_tidy']}; kinds: {', '.join(facts['kinds']) or 'none'}"
                                    + _waiting_text(facts),
                                    orch_facts_baselines.human_line(facts, language),
                                    *orch_role_schedule.human_lines(facts["role_schedule"], language),
                                    *_ci_alarm_lines(facts, language),
                                    *orch_facts_reviews.cost_lines(facts, language, HUMAN_ITEMS),
                                    *_decided_lines(facts, language)]]
    return "\n\n".join("\n".join(block) for block in blocks)


def add_parser(orch_actions) -> None:
    """`fa orch facts [--json]` under `fa orch`."""
    facts = orch_actions.add_parser("facts", help="Show measured run, slot, file and resource facts")
    facts.add_argument("--repo-root", type=Path, default=Path.cwd())
    facts.add_argument("--json", action="store_true")


def command(args, store: StateStore, config: Config) -> None:
    # The checkout config; collect applies the tick's recorded committed config from its frozen read.
    facts = collect(store, config, args.repo_root.resolve(), checkout=True)
    print(json.dumps(facts, ensure_ascii=False) if args.json else human(facts, config.language or "en"))

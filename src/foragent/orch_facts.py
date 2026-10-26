"""`fa orch facts`: measured facts from local records only (D82).

Every number here is read or computed from files the gate and runners already
wrote: run records and their completion history, the phase journal, the shared
capacity files, recorded plan usage and approved issue snapshots. Nothing is
written, no process is started or signalled and GitHub is never called. The
facts name what binds now; they decide nothing and carry no throughput goal.
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

from foragent import capacity, machine_budget
from foragent.config import Config
from foragent.orch_facts_resources import _binding, _probe, _resources, _still_running
from foragent.orch_facts_runs import (
    RECENT, _comparisons, _executions, _failure_reasons, _history, _hot_files, _merges_since_tidy, _row,
    _slowest_files, _test_slots, _waiting_issues,
)
from foragent.state import Run, StateStore

HUMAN_RUNS = 6
HUMAN_ITEMS = 4


def _current_runs(store: StateStore) -> list[Run]:
    """Each issue's current run record, read once per facts snapshot."""
    runs = []
    for path in sorted((store.root / "runs").glob("*.json")):
        if path.stem.isdigit() and (run := store.get_run(int(path.stem))) is not None:
            runs.append(run)
    return runs


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

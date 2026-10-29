"""The ``kinds`` list of `fa orch facts`: named local facts that role signals can point at (#772).

Each kind is present or absent. Each ``*_numbers`` function returns the numbers that make
its kind present (empty when absent); facts show them as ``kind_numbers`` (#794).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.labels import READY
from foragent.orch_facts_resources import _time
from foragent.state import Run, StateStore

HOLD_WINDOW = timedelta(days=7)
HOLDS = 2
OVERLAP_WINDOW = timedelta(hours=24)
OVERLAP_RUNS = 3
# A module is over its limit above this many lines.
LINE_LIMIT = 500
BACKLOG = 3
CONCENTRATION_WINDOW = timedelta(hours=6)
CONCENTRATION_RUNS = 8
CONCENTRATION_IDLE = 2
KINDS = ("repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog",
         "lane_concentration")


def repeated_design_holds_numbers(store: StateStore, now: datetime) -> dict[str, Any]:
    """Issues with two or more executions held in the last 7 days, from the run history."""
    finished = store.finish_times()
    counts: dict[int, int] = {}
    for run in store.read_history():
        at = _time(finished.get((run.number, run.started_at)))
        if run.outcome == "held" and at is not None and now - HOLD_WINDOW < at <= now:
            counts[run.number] = counts.get(run.number, 0) + 1
    held = {f"#{number}": count for number, count in sorted(counts.items()) if count >= HOLDS}
    return {"holds_by_issue": held} if held else {}


def repeated_design_holds(store: StateStore, now: datetime) -> bool:
    return bool(repeated_design_holds_numbers(store, now))


def hot_overlap_files_numbers(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Files in the overlap records of three or more executions started in the last 24 hours."""
    runs: dict[str, set] = {}
    for row in rows:
        started = _time(row["started_at"])
        if started is None or not now - OVERLAP_WINDOW < started <= now:
            continue
        for record in row["overlap_records"]:
            if isinstance(path := record.get("file"), str):
                runs.setdefault(path, set()).add((row["issue"], row["started_at"]))
    hot = {path: len(keys) for path, keys in sorted(runs.items()) if len(keys) >= OVERLAP_RUNS}
    return {"runs_by_file": hot} if hot else {}


def hot_overlap_files(rows: list[dict[str, Any]], now: datetime) -> bool:
    return bool(hot_overlap_files_numbers(rows, now))


def files_over_limit_numbers(root: Path) -> dict[str, Any]:
    """src/**/*.py modules on disk over LINE_LIMIT lines, read directly."""
    over: dict[str, int] = {}
    for path in sorted(Path(root).glob("src/**/*.py")):
        try:
            with path.open("rb") as stream:
                lines = sum(1 for _ in stream)
        except OSError:
            continue
        if lines > LINE_LIMIT:
            over[path.relative_to(Path(root)).as_posix()] = lines
    return {"limit": LINE_LIMIT, "lines_by_file": over} if over else {}


def files_over_limit(root: Path) -> bool:
    return bool(files_over_limit_numbers(root))


def idle_lanes_with_backlog_numbers(config: Config, running: list[Run], waiting) -> dict[str, Any]:
    """Lanes with no active run while another lane has three or more approved, ready issues waiting."""
    lanes = set(config.lanes) | {"main"}
    busy = {run.lane for run in running}
    backlog: dict[str, int] = {}
    for item in waiting:
        if READY in item.labels:
            backlog[item.lane] = backlog.get(item.lane, 0) + 1
    full = {lane for lane, count in backlog.items() if count >= BACKLOG}
    idle = sorted(lane for lane in lanes - busy if full - {lane})
    return {"idle_lanes": idle, "ready_by_lane": {lane: backlog[lane] for lane in sorted(full)}} if idle else {}


def idle_lanes_with_backlog(config: Config, running: list[Run], waiting) -> bool:
    return bool(idle_lanes_with_backlog_numbers(config, running, waiting))


def lane_concentration_numbers(store: StateStore, config: Config, now: datetime, running: list[Run]) -> dict[str, Any]:
    """In the last 6 hours: 8 or more runs started, one lane started more than half, two other lanes none (#794).

    Every started execution counts once by its start time, whatever its outcome; a retry is its own
    execution. Lanes are the configured lanes that run issues (with main), not the role-only entries.
    """
    from foragent.roles import DEFAULT_ROLES

    lanes = (set(config.lanes) | {"main"}) - set(DEFAULT_ROLES)
    started: dict[tuple[int, str], str] = {}
    for run in [*store.read_history(), *running]:
        at = _time(run.started_at)
        if at is not None and now - CONCENTRATION_WINDOW < at <= now:
            started[run.number, run.started_at] = run.lane
    counts: dict[str, int] = {}
    for lane in started.values():
        counts[lane] = counts.get(lane, 0) + 1
    total = len(started)
    if total < CONCENTRATION_RUNS:
        return {}
    for lane, count in sorted(counts.items()):
        idle = sorted(lanes - {lane} - set(counts))
        if count * 2 > total and len(idle) >= CONCENTRATION_IDLE:
            return {"runs": total, "lane": lane, "lane_runs": count, "idle_lanes": idle}
    return {}


def lane_concentration(store: StateStore, config: Config, now: datetime, running: list[Run]) -> bool:
    return bool(lane_concentration_numbers(store, config, now, running))


def collect(store: StateStore, config: Config, root, now: datetime, rows, running, waiting) -> dict[str, Any]:
    """``kinds`` (present kinds, in KINDS order) and ``kind_numbers`` (each present kind's numbers)."""
    numbers = {
        "repeated_design_holds": repeated_design_holds_numbers(store, now),
        "hot_overlap_files": hot_overlap_files_numbers(rows, now),
        "files_over_limit": files_over_limit_numbers(root),
        "idle_lanes_with_backlog": idle_lanes_with_backlog_numbers(config, running, waiting),
        "lane_concentration": lane_concentration_numbers(store, config, now, running),
    }
    present = [kind for kind in KINDS if numbers[kind]]
    return {"kinds": present, "kind_numbers": {kind: numbers[kind] for kind in present}}

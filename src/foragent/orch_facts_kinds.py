"""The ``kinds`` list of `fa orch facts`: named local facts that role signals can point at (#772).

Each kind is present or absent; the numbers behind it stay in the rest of the facts.
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
KINDS = ("repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog")


def repeated_design_holds(store: StateStore, now: datetime) -> bool:
    """One issue with two or more executions held in the last 7 days, from the run history."""
    finished = store.finish_times()
    counts: dict[int, int] = {}
    for run in store.read_history():
        at = _time(finished.get((run.number, run.started_at)))
        if run.outcome == "held" and at is not None and now - HOLD_WINDOW < at <= now:
            counts[run.number] = counts.get(run.number, 0) + 1
    return any(count >= HOLDS for count in counts.values())


def hot_overlap_files(rows: list[dict[str, Any]], now: datetime) -> bool:
    """A file in the overlap records of three or more executions started in the last 24 hours."""
    runs: dict[str, set] = {}
    for row in rows:
        started = _time(row["started_at"])
        if started is None or not now - OVERLAP_WINDOW < started <= now:
            continue
        for record in row["overlap_records"]:
            if isinstance(path := record.get("file"), str):
                runs.setdefault(path, set()).add((row["issue"], row["started_at"]))
    return any(len(keys) >= OVERLAP_RUNS for keys in runs.values())


def files_over_limit(root: Path) -> bool:
    """A src/**/*.py module on disk over LINE_LIMIT lines, read directly."""
    for path in sorted(Path(root).glob("src/**/*.py")):
        try:
            with path.open("rb") as stream:
                if sum(1 for _ in stream) > LINE_LIMIT:
                    return True
        except OSError:
            continue
    return False


def idle_lanes_with_backlog(config: Config, running: list[Run], waiting) -> bool:
    """A lane with no active run while another lane has three or more approved, ready issues waiting."""
    lanes = set(config.lanes) | {"main"}
    busy = {run.lane for run in running}
    backlog: dict[str, int] = {}
    for item in waiting:
        if READY in item.labels:
            backlog[item.lane] = backlog.get(item.lane, 0) + 1
    full = {lane for lane, count in backlog.items() if count >= BACKLOG}
    return any(full - {lane} for lane in lanes - busy)


def collect(store: StateStore, config: Config, root, now: datetime, rows, running, waiting) -> list[str]:
    present = {
        "repeated_design_holds": repeated_design_holds(store, now),
        "hot_overlap_files": hot_overlap_files(rows, now),
        "files_over_limit": files_over_limit(root),
        "idle_lanes_with_backlog": idle_lanes_with_backlog(config, running, waiting),
    }
    return [kind for kind in KINDS if present[kind]]

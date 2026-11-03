"""The ``kinds`` list of `fa orch facts`: named local facts that role signals can point at (#772).

Each kind is present or absent. Each ``*_numbers`` function returns the numbers that make
its kind present (empty when absent); facts show them as ``kind_numbers`` (#794).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from foragent import orch_scorecard_history
from foragent.config import Config
from foragent.labels import READY
from foragent.orch_facts_resources import _time
from foragent.state import Run, StateStore
from foragent.state_observations import observed_facts

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
# The recent merge interval is the median gap between this many latest merges.
STALL_MERGES = 4
STALL_FACTOR = 3
KINDS = ("repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog",
         "lane_concentration", "merge_stall", "waiting_on_person")
WAITING_LABEL = "needs-human"
# Gate alarms (#1469): the gate calls the architect on these directly, whatever its configured
# signals. Repeated holds and idle lanes with ready work reach it through its default signals.
ALARM_KINDS = ("merge_stall",)
# Numbers that grow with time alone. A comparison of burdens leaves them out, so elapsed time never
# makes a signal worse (#1469); the facts still show them.
ELAPSED_FACTS = {"merge_stall": ("minutes_since_merge", "merge_interval_minutes"),
                 "waiting_on_person": ("oldest_waiting_seconds",)}
# Numbers that give a burden its scale but are no burden themselves (#1469): a burden comparison
# leaves them out. A larger total makes lane concentration smaller, not worse, so its burden is
# the share ``SHARE_FACTS`` derives; a file limit is the rule, not a load.
CONTEXT_FACTS = {
    "files_over_limit": ("limit",),
    "lane_concentration": ("runs", "lane_runs"),
}
# A burden field derived as part / whole from a kind's numbers: name -> (part, whole). A share is
# worse when it rises; every other count only when it at least doubles.
SHARE_FACTS = {"lane_concentration": {"share": ("lane_runs", "runs")}}
# Counts that are worse on any rise above the baseline, not only on doubling (#1733): one more
# person-wait is a new burden. A different ``oldest_issue`` is worse as any changed value is.
RISING_FACTS = {"waiting_on_person": ("waiting",)}
# Kinds re-raised by a worse burden alone, measured against the facts of the last raise, never
# those at its answer (#1733): a clear (the kind absent for a tick) never re-arms them, so a wait
# that ends and the same oldest item returning at the same count stay quiet.
EVIDENCE_ONLY = frozenset({"waiting_on_person"})
# Top-level `fa orch facts` fields a kind is about besides its own numbers (#1155). Idle lanes
# with ready work are explained by the binding constraint; every other kind is only its numbers.
SHARED_FACTS: dict[str, tuple[str, ...]] = {"idle_lanes_with_backlog": ("binding",)}
# Kinds measured over a sliding window: kind -> (window, threshold) (#1840). Until that window
# has passed since the last answer, a member newly reaching the threshold is drift of the same
# window, not new evidence; it is worse only at double the threshold, as an existing member's
# count is. After the window a new member is worse as usual.
WINDOW_FACTS: dict[str, tuple[timedelta, int]] = {"hot_overlap_files": (OVERLAP_WINDOW, OVERLAP_RUNS)}


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


def merge_stall_numbers(store: StateStore, now: datetime, running: list[Run]) -> dict[str, Any]:
    """Open issues wait while no merge came for three recent merge intervals (#1469).

    The interval is the median gap between the latest merges; with fewer than two there is none.
    Any open issue not running counts as queued, approved or not: none of them can merge.
    """
    finished = store.finish_times()
    merges = sorted(at for run in store.read_history() if run.outcome == "merged"
                    and (at := _time(finished.get((run.number, run.started_at)))) is not None and at <= now)
    merges = merges[-STALL_MERGES:]
    if len(merges) < 2:
        return {}
    interval = median(later - earlier for earlier, later in zip(merges, merges[1:]))
    since = now - merges[-1]
    active = {run.number for run in running}
    queued = [issue["number"] for issue in store.local_issues()
              if issue.get("state") == "open" and not issue.get("is_pull_request") and issue["number"] not in active]
    if not queued or interval <= timedelta() or since < STALL_FACTOR * interval:
        return {}
    return {"queued": len(queued), "minutes_since_merge": round(since.total_seconds() / 60),
            "merge_interval_minutes": round(interval.total_seconds() / 60)}


def _waits(labels: Any, state: Any) -> bool:
    return state == "open" and WAITING_LABEL in (labels or ())


def waiting_on_person_numbers(store: StateStore, now: datetime) -> dict[str, Any]:
    """Open issues labelled needs-human, from observed issue state only (#1733).

    The current observation (each stored issue row) says who waits. The retained issue-state
    history says since when: the first record of the latest unbroken run of records showing
    the issue open with the label. When trimming cut that run, its earliest kept record counts,
    and an issue with no such record yet counts from its row's observation; both are lower bounds.
    """
    history = orch_scorecard_history.records(store)
    starts: dict[int, datetime] = {}
    for key, row in store._issue_view()["issues"].items():
        if not key.isdigit() or row.get("is_pull_request") or row.get("observed_at") is None:
            continue
        facts = observed_facts(row)
        if not _waits(facts["labels"], facts["state"]):
            continue
        start = None
        for at, data in reversed(history.get(int(key), [])):
            if not _waits(data.get("labels"), data.get("state")):
                break
            start = at
        if (start := start or _time(row["observed_at"])) is not None:
            starts[int(key)] = start
    if not starts:
        return {}
    number, start = min(starts.items(), key=lambda item: (item[1], item[0]))
    return {"waiting": len(starts), "oldest_issue": f"#{number}",
            "oldest_waiting_seconds": max(0, int((now - start).total_seconds()))}


def collect(store: StateStore, config: Config, root, now: datetime, rows, running, waiting) -> dict[str, Any]:
    """``kinds`` (present kinds, in KINDS order) and ``kind_numbers`` (each present kind's numbers)."""
    numbers = {
        "repeated_design_holds": repeated_design_holds_numbers(store, now),
        "hot_overlap_files": hot_overlap_files_numbers(rows, now),
        "files_over_limit": files_over_limit_numbers(root),
        "idle_lanes_with_backlog": idle_lanes_with_backlog_numbers(config, running, waiting),
        "lane_concentration": lane_concentration_numbers(store, config, now, running),
        "merge_stall": merge_stall_numbers(store, now, running),
        "waiting_on_person": waiting_on_person_numbers(store, now),
    }
    present = [kind for kind in KINDS if numbers[kind]]
    return {"kinds": present, "kind_numbers": {kind: numbers[kind] for kind in present}}

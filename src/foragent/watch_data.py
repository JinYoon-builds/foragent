"""Lane-view snapshots, order progress and recent events.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from foragent import orch_inbox
from foragent.config import Config
from foragent.labels import BLOCKED, NEEDS_HUMAN
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker
from foragent.watch_sections import progress


@dataclass
class Snapshot:
    status: dict[str, Any]
    done: dict[str, list[Issue]]
    order: Issue | None
    completed: int
    total: int
    eta: datetime | None
    events: list[dict[str, Any]]
    captured_at: datetime
    # The durable request box (D76), oldest first.
    requests: list[dict[str, Any]] = field(default_factory=list)
    # Every open order together: their count, merges in the last three hours, and quota by executor.
    orders: int = 0
    recent: int = 0
    quota: dict[str, tuple[float, datetime]] = field(default_factory=dict)
    # Evidence ages in seconds at captured_at (D67): heartbeat files by writer
    # name ("orch", "gate") and runner logs by issue number. Missing evidence is absent.
    heartbeats: dict[str, float] = field(default_factory=dict)
    log_ages: dict[int, float] = field(default_factory=dict)
    # Each running worker's own state from its issue labels, with the same
    # precedence as status.collect_lanes: needs-human, then blocked, then working.
    worker_states: dict[int, str] = field(default_factory=dict)
    # The finish estimate's spread (earliest, latest), the merged runs behind each
    # remaining size (None while it uses the default), and merges since local midnight.
    eta_range: tuple[datetime, datetime] | None = None
    eta_basis: dict[str, int | None] = field(default_factory=dict)
    today: int = 0


def recent_events(store: StateStore) -> list[dict[str, Any]]:
    """Ignore an incomplete append without taking the writer's lock."""
    from foragent import watch

    events: deque[dict[str, Any]] = watch.deque(maxlen=5)
    try:
        with (store.root / "orch-events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = watch.json.loads(line)
                except watch.json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("summary"), str):
                    events.append(event)
    except FileNotFoundError:
        pass
    return list(reversed(events))


def evidence(store: StateStore, numbers: list[int], now: datetime) -> tuple[dict[str, float], dict[int, float]]:
    """Ages of the heartbeat files and of the runner logs, read from the files themselves."""
    heartbeats: dict[str, float] = {}
    for name in ("orch", "gate"):
        try:
            at = datetime.fromisoformat(json.loads(
                (store.root / f"{name}-heartbeat.json").read_text(encoding="utf-8"))["at"])
            heartbeats[name] = max(0.0, (now - at).total_seconds())
        except (OSError, ValueError, KeyError, TypeError):
            pass  # No readable heartbeat is no evidence of life.
    logs: dict[int, float] = {}
    for number in numbers:
        try:
            modified = store.log_path(number).stat().st_mtime
        except OSError:
            continue
        logs[number] = max(0.0, now.timestamp() - modified)
    return heartbeats, logs


def collect(
    status: dict[str, Any], tracker: Tracker, store: StateStore, config: Config, now: datetime,
) -> Snapshot:
    """Supplement the shared status JSON with order edges and completions from local records.

    The tracker is accepted for compatibility; refreshes never read it.
    """
    from foragent import watch

    rows = {row["number"]: row for row in store.local_issues()}
    issues = {number: Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"])
              for number, row in rows.items()}

    def sub_issues(number: int) -> list[Issue]:
        return [issues[child] for child, row in rows.items() if row.get("parent") == number]

    lanes = status["lanes"]
    # Prefer the order currently executing, then other visible work. Never add
    # progress from a different order into this order's denominator.
    candidates = [run["number"] for run in status["active_runs"]]
    candidates += [row["issue"] for row in lanes.values() if row["issue"] is not None]
    candidates += [issue.number for issue in issues.values() if issue.state == "open"]
    order = None
    for number in dict.fromkeys(candidates):
        parent = rows.get(number, {}).get("parent")
        if parent is not None and (candidate := issues.get(parent)) is not None and candidate.state == "open":
            order = candidate
            break
    children = sub_issues(order.number) if order else []
    if order is None:
        # An order can remain open after its last child has finished.
        for candidate in sorted(issues.values(), key=lambda issue: issue.number):
            if candidate.state == "open" and (children := sub_issues(candidate.number)):
                order = candidate
                break
    # One bar sums every open order; the representative order above only names the work.
    completed, total, orders = progress.totals(list(rows.values()))
    open_orders = {number for number, issue in issues.items() if issue.state == "open"}
    child_numbers = {number for number, row in rows.items() if row.get("parent") in open_orders}
    history = store.read_history()  # status.history deliberately contains only ten entries
    merged = {}
    for run in history:
        if run.outcome == "merged":
            merged.pop(run.number, None)
            merged[run.number] = run
    done: dict[str, list[Issue]] = {}
    for number, run in merged.items():
        if child_numbers and number not in child_numbers:
            continue
        if (issue := issues.get(number)) is not None:
            done.setdefault(run.lane, []).append(issue)
    finished = [end for *_, end in progress.merged_work(store) if end <= now]
    recent = sum(now - progress.RATE_WINDOW <= end for end in finished)
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = sum(midnight <= end for end in finished)
    sizes = [progress.size_of(rows[number]["labels"]) for number in sorted(child_numbers)
             if rows[number]["state"] == "open" and progress.APPROVED in rows[number]["labels"]]
    found = progress.estimate(sizes, progress.learned(progress.machine_stores(store), now),
                              progress.parallel_budget(store, config), now)
    eta, eta_range, basis = (found[0], found[1:3], found[3]) if found else (None, None, {})
    try:
        requests = orch_inbox.requests(store)
    except (OSError, ValueError):
        requests = []  # An unreadable box must not take the whole view down.
    heartbeats, logs = evidence(store, [run["number"] for run in status["active_runs"]], now)
    worker_states = {}
    for run in status["active_runs"]:
        labels = issues[run["number"]].labels if run["number"] in issues else frozenset()
        worker_states[run["number"]] = ("waiting-human" if NEEDS_HUMAN in labels
                                        else "blocked" if BLOCKED in labels else "working")
    return watch.Snapshot(status, done, order, completed, total, eta, watch.recent_events(store), now,
                          requests, orders, recent, progress.quota(store.root / "logs", now), heartbeats, logs,
                          worker_states, eta_range, basis, today)

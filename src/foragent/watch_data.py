"""Lane-view snapshots, order progress and recent events.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker


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


def collect(
    status: dict[str, Any], tracker: Tracker, store: StateStore, config: Config, now: datetime,
) -> Snapshot:
    """Supplement the shared status JSON with native order edges and completions."""
    from foragent import watch

    issues = {issue.number: issue for issue in tracker.list_issues()}
    rows = status["lanes"]
    # Prefer the order currently executing, then other visible work. Never add
    # progress from a different order into this order's denominator.
    candidates = [run["number"] for run in status["active_runs"]]
    candidates += [row["issue"] for row in rows.values() if row["issue"] is not None]
    candidates += [issue.number for issue in issues.values() if issue.state == "open"]
    order = None
    for number in dict.fromkeys(candidates):
        parent = tracker.parent_of(number)
        if parent is not None:
            candidate = issues.get(parent) or tracker.get_issue(parent)
            if candidate.state == "open":
                order = candidate
                break
    children = tracker.sub_issues(order.number) if order else []
    if order is None:
        # An order can remain open after its last child has finished.
        for candidate in sorted(issues.values(), key=lambda issue: issue.number):
            if candidate.state == "open" and (children := tracker.sub_issues(candidate.number)):
                order = candidate
                break
    child_numbers = {issue.number for issue in children}
    completed = sum(issue.state == "closed" for issue in children)
    history = store.read_history()  # status.history deliberately contains only ten entries
    merged = {}
    for run in history:
        if run.outcome == "merged":
            merged.pop(run.number, None)
            merged[run.number] = run
    done: dict[str, list[Issue]] = {}
    durations = []
    for number, run in merged.items():
        if order is not None and number not in child_numbers:
            continue
        issue = issues.get(number)
        if issue is None:
            continue
        done.setdefault(run.lane, []).append(issue)
        duration = (watch.datetime.fromisoformat(run.last_progress_at) - watch.datetime.fromisoformat(run.started_at)).total_seconds()
        if duration > 0:
            durations.append(duration)
    eta = None
    if order is not None and durations and completed < len(children):
        # A lane is serial; max_parallel also caps total throughput. Historical
        # run duration is an estimate, so the UI explicitly prefixes the time ~.
        workloads = watch.Counter(watch.issue_lane(issue.labels, config.lanes) for issue in children if issue.state == "open")
        batches = max(max(workloads.values()), sum(workloads.values()) / config.max_parallel)
        eta = now + watch.timedelta(seconds=batches * sum(durations) / len(durations))
    return watch.Snapshot(status, done, order, completed, len(children), eta, watch.recent_events(store), now)

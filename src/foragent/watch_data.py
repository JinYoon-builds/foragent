"""Lane-view snapshots, order progress and recent events.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from foragent import orch_inbox, watch_lane_facts, watch_role_facts
from foragent.config import Config
from foragent.labels import issue_lane
from foragent.status import issue_state
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker
from foragent.watch_sections import orch_panel, progress


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
    # When each issue in ``done`` merged, from the local issue records; unknown times are absent.
    merged_at: dict[int, datetime] = field(default_factory=dict)
    # The orchestrator note, its age in seconds and the fresh `fa inspect` count (orch_panel.collect).
    panel: dict[str, Any] = field(default_factory=dict)
    # Closed phase events (kind=phase, #224) by issue, and the latest journal event with a say line.
    phases: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    say: dict[str, Any] | None = None
    # From the local issue records and the full run history (status.history keeps only ten):
    # each worker's title, each issue's latest recorded run start, and today's merges by lane
    # (None when not collected; the lane rows then fall back to the order's done list).
    titles: dict[int, str] = field(default_factory=dict)
    run_starts: dict[int, str] = field(default_factory=dict)
    # The phase each issue's latest recorded run was in when it stopped (finish_run keeps it).
    run_phases: dict[int, str | None] = field(default_factory=dict)
    today_done: dict[str, int] | None = None
    # Order preparation facts from ``fa order progress`` (StateStore.order_progress); None when none.
    prep: dict[str, Any] | None = None
    # The usage line's facts, read once per refresh (``meter``): usage.json, the machine's
    # running runs and budget, the effective Claude mode and memory pressure. Empty when not collected.
    meter: dict[str, Any] = field(default_factory=dict)
    # Per-lane, per-role, per-waiting-issue and per-parked-issue facts for the lane blocks
    # and their explanations. Empty until a collector fills them; manual snapshots stay safe.
    lane_facts: dict[str, dict] = field(default_factory=dict)
    role_facts: dict[str, dict] = field(default_factory=dict)
    waiting_facts: dict[int, dict] = field(default_factory=dict)
    park_facts: dict[int, dict] = field(default_factory=dict)


# The checkout a refresh reads root-dependent facts under. watch.run binds it around its
# five-argument collect call, so the facade's patch point keeps its shape.
COLLECTION_ROOT: ContextVar[Path | None] = ContextVar("COLLECTION_ROOT", default=None)


@contextmanager
def collection_root(root: Path) -> Iterator[Path]:
    """Bind the checkout root for collect calls in this context, restoring it afterwards."""
    token = COLLECTION_ROOT.set(root)
    try:
        yield root
    finally:
        COLLECTION_ROOT.reset(token)


def meter(store: StateStore, config: Config, now: datetime) -> dict[str, Any]:
    """One read of what the usage line shows; an unreadable record reads as missing."""
    from foragent import capacity, scheduler_quota

    found: dict[str, Any] = {"mode": scheduler_quota.quota_mode(store, config)[0],
                             "memory": capacity.memory_pressure()}
    try:
        usage = store.usage()
    except (OSError, ValueError):
        usage = {}
    found["usage"] = usage if isinstance(usage, dict) else {}
    state = capacity.read_state(store)
    found["budget"] = config.max_parallel or (int(state["parallel"]) if state else capacity.INITIAL_PARALLEL)
    try:
        found["running"] = capacity.machine_runs(store, now)
    except OSError:
        pass  # No machine state folder yet: the line counts this repository's active runs.
    return found


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
    except OSError:
        pass  # Missing or unreadable: no recent events.
    return list(reversed(events))


def journal_facts(store: StateStore) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any] | None]:
    """Every closed phase event by issue and the latest event with a say line, from the whole journal."""
    from foragent import watch

    phases: dict[int, list[dict[str, Any]]] = {}
    say = None
    try:
        with (store.root / "orch-events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = watch.json.loads(line)
                except watch.json.JSONDecodeError:
                    continue  # An append still being written.
                if not isinstance(event, dict):
                    continue
                if event.get("kind") == "phase" and isinstance(event.get("issue"), int) and event.get("phase"):
                    phases.setdefault(event["issue"], []).append(event)
                if isinstance(event.get("say"), str) and event["say"].strip():
                    say = event
    except OSError:
        pass  # Missing or unreadable: no phase events and no say line.
    return phases, say


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
    *, repo_root: Path | None = None,
) -> Snapshot:
    """Supplement the shared status JSON with order edges and completions from local records.

    The tracker is accepted for compatibility; refreshes never read it. ``repo_root`` is
    the checkout for root-dependent facts: an explicit value wins, else the bound
    COLLECTION_ROOT; unbound it stays None (never the cwd or the state folder's parent).
    """
    from foragent import watch

    repo_root = repo_root if repo_root is not None else COLLECTION_ROOT.get()

    try:
        rows = {row["number"]: row for row in store.local_issues()}
    except (OSError, ValueError):
        rows = {}  # An unreadable issue snapshot provides no authored waits or other issue facts.
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
    try:
        history = store.read_history()  # status.history deliberately contains only ten entries
    except (OSError, ValueError):
        history = None  # Unreadable history: no completions or week counts, not zero ones.
    merged = {}
    for run in history or []:
        if run.outcome == "merged":
            merged.pop(run.number, None)
            merged[run.number] = run
    done: dict[str, list[Issue]] = {}
    for number, run in merged.items():
        if child_numbers and number not in child_numbers:
            continue
        if (issue := issues.get(number)) is not None:
            done.setdefault(run.lane, []).append(issue)
    merged_at = {}
    for issues_done in done.values():
        for issue in issues_done:
            at = progress._time(rows[issue.number].get("merged_at"))
            if at is not None:  # Unseen merges are listed after the known times.
                merged_at[issue.number] = at if at.tzinfo else at.replace(tzinfo=UTC)
    try:
        work = progress.merged_work(store)
    except (OSError, ValueError):
        work = None  # merged_work reads the same history; unreadable means unknown.
    merges = [(number, end) for number, *_, end in work or [] if end <= now]
    finished = [end for _, end in merges]
    recent = sum(now - progress.RATE_WINDOW <= end for end in finished)
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = sum(midnight <= end for end in finished)
    run_lanes = {run.number: run.lane for run in history or []}  # The latest run names the lane.
    today_done: dict[str, int] | None = {} if work is not None else None
    for number, end in merges:
        if midnight <= end:
            lane = run_lanes.get(number) or issue_lane(rows.get(number, {}).get("labels", ()), config.lanes)
            today_done[lane] = today_done.get(lane, 0) + 1
    latest_runs = {run.number: run for run in history or []}  # History is oldest first; the last run wins.
    run_starts = {number: run.started_at for number, run in latest_runs.items() if run.started_at}
    run_phases = {number: run.phase for number, run in latest_runs.items()}
    titles = {number: issue.title for number, issue in issues.items()}
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
        # The same mapping as status.collect_lanes, for a running worker.
        worker_states[run["number"]] = issue_state(labels, True)
    phases, say = journal_facts(store)
    try:
        prep = store.order_progress()
    except (OSError, ValueError):
        prep = None  # A half-written record must not take the whole view down.
    if not isinstance(prep, dict):
        prep = None
    published = [number for number in (prep or {}).get("published", []) if isinstance(number, int)]
    shown = {run["number"] for run in status["active_runs"]} | {
        row["issue"] for row in lanes.values() if row.get("issue") is not None} | set(published)
    titles = {number: title for number, title in titles.items() if number in shown}
    found_meter = meter(store, config, now)
    events = watch_lane_facts.journal(store)  # One read shared by the park and review facts.
    merged_at = watch_lane_facts.merged_today(merged_at, work, now)
    return watch.Snapshot(status, done, order, completed, total, eta, watch.recent_events(store), now,
                          requests, orders, recent, progress.quota(store.root / "logs", now), heartbeats, logs,
                          worker_states, eta_range, basis, today, merged_at, orch_panel.collect(store, now),
                          phases, say, titles=titles, run_starts=run_starts, run_phases=run_phases,
                          today_done=today_done, prep=prep,
                          meter=found_meter,
                          lane_facts=watch_lane_facts.lane_facts(status, store, config, now, repo_root,
                                                                 work, events, rows, history),
                          role_facts=watch_role_facts.collect(store),
                          waiting_facts=watch_lane_facts.waiting_facts(status, store, config, now, repo_root,
                                                                       rows, found_meter, history),
                          park_facts=watch_lane_facts.park_facts(events))

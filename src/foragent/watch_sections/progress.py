"""Progress across every open order from local records only.

One bar sums all open orders, the finish time sums each remaining approved issue's
learned duration for its size (with the runs it rests on), and quota is read from
executor logs.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import statistics
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.watch_cells import Line

ORDER = 21
BAR_CELLS = 20
RATE_WINDOW = timedelta(hours=3)
SIZES = ("s", "m", "l")
DEFAULT_MINUTES = {"s": 30, "m": 90, "l": 180}
LEARN_MIN = 3  # Below this many merged runs a size keeps its default.
LEARN_LAST = 30
LEARN_WINDOW = timedelta(days=14)
APPROVED = "approved"
# Logs are append-only: remember each file's parsed prefix (offset and the newest
# reading of each executor window) so a refresh every few seconds reads only new bytes.
# Window = (key, percent, reset epoch); keyed by executor, then by window name.
Window = tuple[tuple[float, float, float], float, float]
Windows = dict[str, dict[str, Window]]
_LOGS: dict[Path, tuple[tuple[int, bytes], int, Windows]] = {}
HEAD_BYTES = 64


def totals(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(completed, total, open orders) over the children of every open order."""
    open_orders = {row["number"] for row in rows if row["state"] == "open"}
    children = [row for row in rows if row.get("parent") in open_orders]
    parents = {row["parent"] for row in children}
    return sum(row["state"] == "closed" for row in children), len(children), len(parents)


def size_of(labels: Iterable[str]) -> str:
    """The issue's size label; none, or several, count as size:m."""
    sizes = {label.removeprefix("size:") for label in labels if label in {f"size:{size}" for size in SIZES}}
    return sizes.pop() if len(sizes) == 1 else "m"


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _finished(run: Any, merged_at: str | None, finished_at: str | None) -> datetime | None:
    """When a gate run merged: the merge moment the runner recorded, else (older
    records) its finish, else its latest recorded time. The finish comes after
    post-merge installation and cleanup, so it only stands in for the merge."""
    start = _time(run.started_at)
    if (at := _time(merged_at)) is not None and (start is None or at >= start):
        return at
    if (at := _time(finished_at)) is not None:
        return at
    times = [at for value in (run.last_progress_at, run.phase_started_at, run.last_activity_at)
             if (at := _time(value)) is not None]
    return max(times, default=None)


def merged_work(store: Any) -> list[tuple[int, str, datetime, datetime]]:
    """Each merged issue of one store once, at its latest merge: (number, size, started, merged).

    The merge starts at the issue's latest recorded run that began before it, for
    gate and outside merges alike. Each merge uses the facts recorded when it was
    first seen, so a later reopening, re-approval or relabelling never adds, removes
    or changes one. A gate merge is a run that finished as merged. A merged PR
    counts when, first seen, it closed an approved issue with a recorded run. The
    gate run's own PR is the same merge seen from GitHub (same run, merged seconds
    from the run's finish), and the issue counts once, so it adds no second sample;
    any later merge, gate or outside, with or without a new run, is the latest."""
    rows = {row["number"]: row for row in store.local_issues()}
    history = store.read_history()
    starts: dict[int, list[datetime]] = {}
    for run in [*history, *store.active_runs()]:
        if (at := _time(run.started_at)) is not None:
            starts.setdefault(run.number, []).append(at)
    latest: dict[int, tuple[datetime, str, datetime | None]] = {}  # number -> (merged, size, started)

    def merged(number: int, end: datetime, size: str, start: datetime | None) -> None:
        if number not in latest or end > latest[number][0]:
            latest[number] = (end, size, start)

    finish_times, merge_times = store.finish_times(), store.merge_times()
    for run in history:
        if run.outcome != "merged":
            continue
        if (end := _finished(run, merge_times.get(key := (run.number, run.started_at)), finish_times.get(key))) is not None:
            row = rows.get(run.number, {})
            # Before any refresh has seen the merge, the issue's current size stands in.
            size = (row.get("run_sizes") or {}).get(run.started_at) or size_of(row.get("labels", ()))
            merged(run.number, end, size, max((at for at in starts.get(run.number, []) if at <= end), default=None))
    for number, row in rows.items():
        for merge in (row.get("merges") or {}).values():
            if not (merge.get("approved") and merge.get("closed")) or (end := _time(merge.get("merged_at"))) is None:
                continue
            if merge.get("size") in SIZES:
                merged(number, end, merge["size"], _time(merge.get("started_at")))
    return [(number, size, start, end) for number, (end, size, start) in sorted(latest.items())
            if start is not None]


def machine_stores(store: Any) -> list[Any]:
    """This store and every repository's store under the machine data root."""
    from foragent import capacity

    stores, seen = [store], {store.root.resolve()}
    try:
        paths = sorted(capacity.machine_root().iterdir())
    except OSError:
        paths = []
    for path in paths:
        if path.is_dir() and path.resolve() not in seen and (
                (path / "runs.jsonl").exists() or (path / "issues.json").exists()):
            seen.add(path.resolve())
            stores.append(type(store)(path))
    return stores


def learned(stores: Iterable[Any], now: datetime) -> dict[str, list[float]]:
    """Seconds from start to merge of each size's last runs merged within the window."""
    samples: dict[str, list[tuple[datetime, float]]] = {size: [] for size in SIZES}
    for store in stores:
        try:
            work = merged_work(store)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # An unreadable repository must not take the estimate down.
        for _, size, start, end in work:
            if now - LEARN_WINDOW <= end <= now and end >= start:
                samples[size].append((end, (end - start).total_seconds()))
    return {size: [seconds for _, seconds in sorted(items, key=lambda item: item[0])[-LEARN_LAST:]]
            for size, items in samples.items()}


def expected(seconds: list[float], size: str) -> tuple[float, float, float, int | None]:
    """(25th percentile, median, 75th percentile, runs) or the size's default band."""
    if len(seconds) >= LEARN_MIN:
        low, _, high = statistics.quantiles(seconds, n=4, method="inclusive")
        return low, statistics.median(seconds), high, len(seconds)
    default = DEFAULT_MINUTES[size] * 60.0
    return default / 2, default, default * 2, None


def parallel_budget(store: Any, config: Config) -> int:
    """The machine's current parallel budget, else the configured ceiling, else 1."""
    from foragent import capacity

    state = capacity.read_state(store)
    value = int(state["parallel"]) if state else (config.max_parallel or 1)
    return max(1, value)


def estimate(
    sizes: list[str], samples: dict[str, list[float]], budget: int, now: datetime,
) -> tuple[datetime, datetime, datetime, dict[str, int | None]] | None:
    """(finish, earliest, latest, runs per size) for the remaining sizes, or None without work."""
    if not sizes:
        return None
    bands = {size: expected(samples.get(size, []), size) for size in SIZES if size in sizes}
    low, mid, high = (sum(bands[size][index] for size in sizes) / budget for index in range(3))
    basis = {size: band[3] for size, band in bands.items()}
    return (now + timedelta(seconds=mid), now + timedelta(seconds=low), now + timedelta(seconds=high), basis)


def _windows(executor: str, event: dict[str, Any]) -> list[tuple[str, float, float]]:
    """Every (window name, percent, reset epoch) of one executor usage event."""
    windows: list[tuple[str, Any, Any]] = []
    if executor == "claude":
        info = event.get("rate_limit_info") or {}
        unified = info.get("unifiedWindows") or {}
        windows = [(str(name), item.get("utilization"), item.get("resetsAt"))
                   for name, item in unified.items() if isinstance(item, dict)]
        if not windows and "utilization" in info:
            windows = [(str(info.get("rateLimitType") or "window"), info.get("utilization"), info.get("resetsAt"))]
        windows = [(name, value * 100 if isinstance(value, (int, float)) else None, reset)
                   for name, value, reset in windows]
    else:
        limits = (event.get("payload") or {}).get("rate_limits") or {}
        windows = [(name, item.get("used_percent"), item.get("resets_at")) for name in ("primary", "secondary")
                   if isinstance(item := limits.get(name), dict)]
    return [(name, float(value), float(reset)) for name, value, reset in windows
            if isinstance(value, (int, float)) and isinstance(reset, (int, float))]


def _executor(event: Any) -> str | None:
    if not isinstance(event, dict):
        return None
    if event.get("type") == "rate_limit_event":
        return "claude"
    payload = event.get("payload")
    if (event.get("type") == "event_msg" and isinstance(payload, dict)
            and payload.get("type") == "token_count" and payload.get("rate_limits")):
        return "codex"
    return None


def _reading_time(event: dict[str, Any]) -> float:
    """The event's own time in epoch seconds, or -inf when it records none."""
    value = event.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            return float("-inf")
        return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()
    return float("-inf")


def _newer(found: dict[str, Window], name: str, window: Window) -> None:
    if name not in found or window[0] >= found[name][0]:
        found[name] = window


def _parse(line: bytes, latest: Windows) -> None:
    """Keep the newest reading of each window separately. Without its own time (Claude
    records none), a later reset means a later window and usage within one window only
    grows until it resets. Windows are never ranked together: an old five-hour peak must
    not make a whole reading look newer than one taken after that window reset."""
    if b'"rate_limit' not in line:
        return
    try:
        event = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return
    if executor := _executor(event):
        at = _reading_time(event)
        for name, percent, reset in _windows(executor, event):
            _newer(latest.setdefault(executor, {}), name, ((at, reset, percent), percent, reset))


def _log_windows(path: Path, inode: int, size: int) -> Windows:
    """The newest reading of each executor window in one whole log, reading only appended bytes."""
    with path.open("rb") as stream:
        identity = (inode, stream.read(HEAD_BYTES))
        cached, offset, known = _LOGS.get(path, (identity, 0, {}))
        if cached != identity or size < offset:
            offset, known = 0, {}  # replaced, rewritten or truncated: read it all again
        latest = {executor: dict(found) for executor, found in known.items()}
        stream.seek(offset)
        data = stream.read(max(0, size - offset))
    complete = data.rfind(b"\n") + 1
    for line in data[:complete].splitlines():
        _parse(line, latest)
    _LOGS[path] = (identity, offset + complete, {executor: dict(found) for executor, found in latest.items()})
    # A line still being written counts now but is parsed again once it is complete.
    _parse(data[complete:], latest)
    return latest


def quota(logs: Path, now: datetime) -> dict[str, tuple[float, datetime]]:
    """Newest recorded usage per executor across every log; reset windows are dropped.

    Readings are ordered by their own time, never by the log file's modification
    time: ordinary output appended to a log does not make its old reading newer."""
    files = []
    try:
        for path in logs.glob("*.log"):
            try:
                files.append((path.stat(), path))
            except OSError:
                continue  # removed between listing and reading
    except OSError:
        return {}
    present = {path for _, path in files}
    for path in [path for path in _LOGS if path.parent == logs and path not in present]:
        del _LOGS[path]
    newest: Windows = {}
    for stat, path in sorted(files, key=lambda item: item[1].name):
        try:
            found = _log_windows(path, stat.st_ino, stat.st_size)
        except OSError:
            continue
        for executor, windows in found.items():
            for name, window in windows.items():
                _newer(newest.setdefault(executor, {}), name, window)
    # Drop windows that already reset before choosing the most-used one: an expired
    # primary window must not hide a still-valid secondary window.
    usage: dict[str, tuple[float, datetime]] = {}
    for executor, windows in newest.items():
        current = [(percent, at) for _, percent, reset in windows.values()
                   if (at := datetime.fromtimestamp(reset, UTC)) > now]
        if current:
            usage[executor] = max(current)
    return usage


def _when(at: datetime, now: datetime, language: str, clock: str) -> str:
    local, today = at.astimezone(), now.astimezone().date()
    if local.date() == today:
        return clock
    if local.date() == today + timedelta(days=1):
        return text("progress_tomorrow", language, time=clock)
    return text("progress_on_day", language, month=local.month, day=local.day, time=clock)


def _moment(at: datetime, now: datetime, language: str) -> str:
    local = at.astimezone()
    return _when(at, now, language, text("progress_clock", language, hour=local.hour, minute=local.minute))


def render(snapshot: Any, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    language = config.language
    now = context.get("now") or snapshot.captured_at
    lines: list[Line] = []
    if snapshot.total:
        filled = round(BAR_CELLS * snapshot.completed / snapshot.total)
        bar = "▓" * filled + "░" * (BAR_CELLS - filled)
        lines.append([(text("progress_bar", language, bar=bar, completed=snapshot.completed,
                                 total=snapshot.total, orders=snapshot.orders), "green")])
        remaining = snapshot.total - snapshot.completed
        # A finished order has no line here: the done section announces it.
        if remaining > 0:
            # Without remaining approved work there is no estimate line.
            if snapshot.eta is not None:
                low, high = snapshot.eta_range or (snapshot.eta, snapshot.eta)
                lines.append([(text("progress_finish", language, low=_moment(low, now, language),
                                    high=_moment(high, now, language)), "bold")])
                items = [text("progress_basis_runs", language, size=size, count=count) if count is not None
                         else text("progress_basis_default", language, size=size)
                         for size, count in snapshot.eta_basis.items()]
                lines.append([(text("progress_basis", language, items=" · ".join(items)), "")])
            if snapshot.recent:
                lines.append([(text("progress_evidence", language, count=snapshot.recent, remaining=remaining), "")])
            else:
                lines.append([(text("progress_no_evidence", language, count=0, remaining=remaining), "")])
                if snapshot.today:
                    lines.append([(text("progress_today", language, count=snapshot.today), "")])
                # In the frame each running issue already has its own lane row (#167).
                running = [] if context.get("rows") else [
                    f"#{run['number']}" + (f" {text(run['phase'], language)}" if run.get("phase") else "")
                    for run in snapshot.status.get("active_runs", [])]
                if running:
                    lines.append([(text("progress_running", language, items=" · ".join(running)), "")])
    if snapshot.quota:
        items = []
        for executor, (percent, reset) in sorted(snapshot.quota.items()):
            clock = reset.astimezone().strftime("%H:%M")
            items.append(text("progress_quota_item", language, executor=executor.capitalize(),
                              percent=f"{round(percent, 1):g}", when=_when(reset, now, language, clock)))
        lines.append([(text("progress_quota", language, items=" · ".join(items)), "")])
    return lines

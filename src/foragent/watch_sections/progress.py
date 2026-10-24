"""Progress across every open order from local records only.

One bar sums all open orders, the finish time comes from the recent merge rate
(with the evidence it rests on), and quota is read from executor logs.
"""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.watch_cells import Line

ORDER = 15
BAR_CELLS = 20
RATE_WINDOW = timedelta(hours=1)
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


def recent_merges(history: list[Any], now: datetime, window: timedelta = RATE_WINDOW) -> int:
    """Issues whose latest merged run finished within the window before now."""
    finished: dict[int, datetime] = {}
    for run in history:
        if run.outcome != "merged":
            continue
        try:
            finished[run.number] = datetime.fromisoformat(run.last_progress_at)
        except (TypeError, ValueError):
            continue
    return sum(now - window <= at <= now for at in finished.values())


def finish_time(remaining: int, recent: int, now: datetime, window: timedelta = RATE_WINDOW) -> datetime | None:
    if remaining <= 0 or recent <= 0:
        return None
    return now + window * (remaining / recent)


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
        if remaining <= 0:
            lines.append([(text("progress_done", language), "")])
        else:
            if snapshot.eta is not None:
                local = snapshot.eta.astimezone()
                clock = text("progress_clock", language, hour=local.hour, minute=local.minute)
                finish = text("progress_finish", language, when=_when(snapshot.eta, now, language, clock))
            else:
                finish = text("progress_unknown_finish", language)
            lines.append([(finish, "bold")])
            key = "progress_evidence" if snapshot.recent else "progress_no_evidence"
            lines.append([(text(key, language, count=snapshot.recent, remaining=remaining), "")])
    if snapshot.quota:
        items = []
        for executor, (percent, reset) in sorted(snapshot.quota.items()):
            clock = reset.astimezone().strftime("%H:%M")
            items.append(text("progress_quota_item", language, executor=executor.capitalize(),
                              percent=f"{round(percent, 1):g}", when=_when(reset, now, language, clock)))
        lines.append([(text("progress_quota", language, items=" · ".join(items)), "")])
    return lines

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
# reading per executor) so a refresh every few seconds reads only new bytes.
Reading = tuple[tuple[float, float, float], list[tuple[float, float]]]
_LOGS: dict[Path, tuple[tuple[int, bytes], int, dict[str, Reading]]] = {}
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


def _windows(executor: str, event: dict[str, Any]) -> list[tuple[float, float]]:
    """Every (percent, reset epoch) window of one executor usage event."""
    windows: list[tuple[Any, Any]] = []
    if executor == "claude":
        info = event.get("rate_limit_info") or {}
        unified = info.get("unifiedWindows") or {}
        windows = [(item.get("utilization"), item.get("resetsAt")) for item in unified.values()
                   if isinstance(item, dict)]
        if not windows and "utilization" in info:
            windows = [(info.get("utilization"), info.get("resetsAt"))]
        windows = [(value * 100 if isinstance(value, (int, float)) else None, reset) for value, reset in windows]
    else:
        limits = (event.get("payload") or {}).get("rate_limits") or {}
        windows = [(item.get("used_percent"), item.get("resets_at")) for item in
                   (limits.get("primary"), limits.get("secondary")) if isinstance(item, dict)]
    return [(float(value), float(reset)) for value, reset in windows
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


def _key(event: dict[str, Any], windows: list[tuple[float, float]]) -> tuple[float, float, float]:
    """How new a reading is: its own time first; without one (Claude records none), a later
    reset means a later window and, within one window, usage only grows until it resets."""
    return (_reading_time(event), max(reset for _, reset in windows),
            max(percent for percent, _ in windows))


def _parse(line: bytes, latest: dict[str, Reading]) -> None:
    if b'"rate_limit' not in line:
        return
    try:
        event = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return
    if (executor := _executor(event)) and (windows := _windows(executor, event)):
        key = _key(event, windows)
        if executor not in latest or key >= latest[executor][0]:
            latest[executor] = (key, windows)


def _log_windows(path: Path, inode: int, size: int) -> dict[str, Reading]:
    """The newest reading per executor in one whole log, reading only appended bytes."""
    with path.open("rb") as stream:
        identity = (inode, stream.read(HEAD_BYTES))
        cached, offset, known = _LOGS.get(path, (identity, 0, {}))
        if cached != identity or size < offset:
            offset, known = 0, {}  # replaced, rewritten or truncated: read it all again
        latest = dict(known)
        stream.seek(offset)
        data = stream.read(max(0, size - offset))
    complete = data.rfind(b"\n") + 1
    for line in data[:complete].splitlines():
        _parse(line, latest)
    _LOGS[path] = (identity, offset + complete, dict(latest))
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
    newest: dict[str, Reading] = {}
    for stat, path in sorted(files, key=lambda item: item[1].name):
        try:
            found = _log_windows(path, stat.st_ino, stat.st_size)
        except OSError:
            continue
        for executor, reading in found.items():
            if executor not in newest or reading[0] > newest[executor][0]:
                newest[executor] = reading
    # Drop windows that already reset before choosing the most-used one: an expired
    # primary window must not hide a still-valid secondary window.
    usage: dict[str, tuple[float, datetime]] = {}
    for executor, (_, windows) in newest.items():
        current = [(percent, at) for percent, reset in windows
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

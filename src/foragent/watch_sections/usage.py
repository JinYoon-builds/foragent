"""The usage line: plan usage per executor, memory, concurrency and the Claude mode (#115).

It is the frame's last line, directly under the help line, and ends with the quit key;
only the other gates' own section (when any) comes after it.
With every lane idle and quota left, one invite line stands above it. Both stay on a
short screen, and both wrap between items instead of losing their end to the frame width.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 65  # Directly under the help line (60).
GAP = False  # Directly under the help line.
GAP_AFTER = False  # A section after it follows directly, so the line costs one row...
APART = ("gates",)  # ...except the other gates, which keep their own section below it.
KEEP = True  # The usage line stays when the screen is short.

EXECUTORS = (("claude", "Claude"), ("codex", "Codex"))


def _windows(record: Any, now: datetime) -> list[tuple[float, datetime]]:
    """(percent, reset) of each window whose reset is still ahead."""
    windows = record.get("windows") if isinstance(record, dict) else None
    found = []
    for window in windows if isinstance(windows, list) else ():
        if not isinstance(window, dict):
            continue
        percent = window.get("used_percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        try:
            reset = datetime.fromisoformat(window.get("resets_at"))
        except (TypeError, ValueError):
            continue
        if reset > now:
            found.append((float(percent), reset))
    return found


def _when(reset: datetime, now: datetime) -> str:
    """Local HH:MM within 24 hours, else M/D HH:MM."""
    local = reset.astimezone()
    if reset - now < timedelta(hours=24):
        return local.strftime("%H:%M")
    return f"{local.month}/{local.day} {local.strftime('%H:%M')}"


def _age(record: Any, now: datetime, language: str) -> str | None:
    from foragent import gates

    seen = record.get("seen_at") if isinstance(record, dict) else None
    try:
        return gates.age(seen, now, language) if isinstance(seen, str) else None
    except (TypeError, ValueError):
        return None


def _keep_percent(config: Config) -> float | None:
    """Claude's keep percent: the explicit executors list's claude entry, else claude_quota."""
    from foragent.config import executors_explicit

    if executors_explicit(config):
        entry = next((item for item in config.executor_list or [] if item.get("name") == "claude"), {})
        return entry.get("keep_percent")
    return config.claude_quota.get("keep_percent")


def _mode(snapshot: Snapshot, config: Config, state: dict[str, Any] | None) -> str:
    """The effective mode; an `m` press after this snapshot was read wins until the next refresh."""
    from foragent.config import executors_explicit

    pressed = (state or {}).get("quota_mode")
    if pressed and pressed[1] >= snapshot.captured_at:
        return pressed[0]
    return snapshot.meter.get("mode") or ("keep" if executors_explicit(config) else config.claude_quota["mode"])


def _parts(snapshot: Snapshot, config: Config, context: dict[str, Any]) -> tuple[list[str], bool]:
    """The usage line's parts, and whether Claude or Codex is below its cap."""
    from foragent import watch

    language = config.language
    now = context.get("now") or snapshot.captured_at
    meter = snapshot.meter
    usage = meter.get("usage") or {}
    mode = _mode(snapshot, config, context.get("state"))
    keep = _keep_percent(config)
    caps = {"claude": keep if mode == "keep" and keep is not None else 100, "codex": 100}
    parts, left = [], False
    for name, label in EXECUTORS:
        record = usage.get(name)
        windows = _windows(record, now)
        age = _age(record, now, language)
        if windows:
            percent, reset = max(windows, key=lambda window: window[0])
            left = left or percent < caps[name]
            details = " · ".join(value for value in (age, _when(reset, now)) if value)
            parts.append(f"{label} {percent:g}% ({details})")
        else:
            parts.append(f"{label} —" + (f" ({age})" if age else ""))
    memory = meter.get("memory")
    if memory in ("warn", "critical"):
        parts.append(watch.text(f"usage_memory_{memory}", language))
    running = meter.get("running", len(snapshot.status.get("active_runs", [])))
    budget = meter.get("budget") or config.max_parallel or _initial()
    parts.append(watch.text("usage_running", language, running=running, budget=budget))
    parts.append(f"keep {keep:g}%" if mode == "keep" and keep is not None else mode)
    return parts, left


def lines(snapshot: Snapshot, config: Config, context: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(the invite line or None, the usage line's items ending with the quit key)."""
    from foragent import watch

    parts, left = _parts(snapshot, config, context)
    # All lanes idle means no active runs, also in a repository without lanes.
    idle = not snapshot.status.get("active_runs")
    invite = watch.text("usage_idle_invite", config.language) if idle and left else None
    return invite, [*parts, watch.text("usage_quit", config.language)]


def _rows(parts: list[str], width: int) -> list[str]:
    """Join the items with ' · ', starting a new row between items when the next would not fit."""
    from foragent import watch

    rows = [parts[0]]
    for part in parts[1:]:
        joined = f"{rows[-1]} · {part}"
        if watch._cells(joined) <= width:
            rows[-1] = joined
        else:
            rows.append(part)
    return rows


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    width = context.get("width") or 120
    invite, parts = lines(snapshot, config, context)
    rows = watch._wrap(invite, width) if invite else []
    # An item wider than the frame still wraps word by word rather than being clipped.
    for row in _rows(parts, width):
        rows += [watch._line(row)] if watch._cells(row) <= width else watch._wrap(row, width)
    return rows


def _initial() -> int:
    from foragent import capacity

    return capacity.INITIAL_PARALLEL

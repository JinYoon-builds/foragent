"""The usage line: plan usage per executor, memory, concurrency and the Claude mode (#115).

It sits directly under the help line, so it is the frame's last line unless other gates
are listed below it. With every lane idle and quota left,
one invite line stands above it. On a short screen the help section joins the usage
line onto its key letters (``lines``), so this section then draws nothing.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 65  # After the help line (60), before the other gates (70).
GAP = False  # Directly under the help line.
GAP_AFTER = False  # A section after it follows directly, so the line costs one row...
APART = ("gates",)  # ...except the other gates, which keep their own section.
KEEP = True  # The usage line stays when the screen is short.
SHORT = True  # Drawn again with short=True, when the help line carries it.

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


def lines(snapshot: Snapshot, config: Config, context: dict[str, Any]) -> tuple[str | None, str]:
    """(the invite line or None, the usage line)."""
    from foragent import watch

    parts, left = _parts(snapshot, config, context)
    # All lanes idle means no active runs, also in a repository without lanes.
    idle = not snapshot.status.get("active_runs")
    invite = watch.text("usage_idle_invite", config.language) if idle and left else None
    return invite, " · ".join(parts)


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    if context.get("short"):
        return []  # The help line carries the usage line on a short screen.
    return [watch._line(value) for value in lines(snapshot, config, context) if value]


def _initial() -> int:
    from foragent import capacity

    return capacity.INITIAL_PARALLEL

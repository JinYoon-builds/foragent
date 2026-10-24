"""Read plan-usage events that executors already print into their run logs.

Claude prints ``rate_limit_event`` lines and Codex prints ``token_count`` events
with ``rate_limits``. Each event names its own format, so a log can hold both
(for example an implementer log and a reviewer of the other kind).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


def _reset(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return None


def _window_name(minutes: Any) -> str | None:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _claude(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    info = event.get("rate_limit_info")
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(windows, dict):
        return None
    rows = []
    for name, window in windows.items():
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            rows.append({"name": name, "used_percent": round(window["utilization"] * 100, 4),
                         "resets_at": _reset(window.get("resetsAt"))})
    return rows or None


def _codex(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    payload = event.get("payload")
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    rows = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
            continue
        rows.append({"name": _window_name(window.get("window_minutes")) or key,
                     "used_percent": float(window["used_percent"]),
                     "resets_at": _reset(window.get("resets_at"))})
    return rows or None


def latest_usage(text: str) -> dict[str, list[dict[str, Any]]]:
    """The last usage report per executor found in ``text`` (JSON lines)."""
    found: dict[str, list[dict[str, Any]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "rate_limit_event" and (rows := _claude(event)):
            found["claude"] = rows
        elif event.get("type") == "event_msg" and (rows := _codex(event)):
            found["codex"] = rows
    return found


def record_from_log(store: Any, log: Path, offset: int = 0) -> None:
    """Record usage reported after ``offset`` in ``log``; unreadable logs are ignored."""
    try:
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return
    for executor, windows in latest_usage(text).items():
        store.record_usage(executor, windows)

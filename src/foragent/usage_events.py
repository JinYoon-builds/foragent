"""Read plan-usage events that executors already print into their run logs.

Claude prints ``rate_limit_event`` lines and Codex prints ``token_count`` events
with ``rate_limits``. Each event names its own format, so a log can hold both
(for example an implementer log and a reviewer of the other kind).

Parsed windows carry an ``exhausted`` mark used to time a usage-limit pause;
the stored usage keeps only ``name``, ``used_percent`` and ``resets_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _reset(value: Any) -> str | None:
    value = _number(value)
    if value is None or not value > 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _window_name(minutes: Any) -> str | None:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _window(name: str, used_percent: float | None, resets_at: str | None, *, exhausted: bool = False) -> dict:
    exhausted = exhausted or (used_percent is not None and used_percent >= 100)
    return {"name": name, "used_percent": used_percent, "resets_at": resets_at, "exhausted": exhausted}


def _percent(utilization: Any) -> float | None:
    value = _number(utilization)
    return None if value is None else round(value * 100, 4)


def _claude(info: dict[str, Any]) -> list[dict]:
    rejected = info.get("status") == "rejected"
    limit_type = info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None
    top_reset = _reset(info.get("resetsAt"))
    rows = []
    unified = info.get("unifiedWindows")
    for name, window in (unified.items() if isinstance(unified, dict) else ()):
        if not isinstance(name, str) or not isinstance(window, dict):
            continue
        resets_at = _reset(window.get("resetsAt"))
        if resets_at is None and name == limit_type:
            resets_at = top_reset
        rows.append(_window(name, _percent(window.get("utilization")), resets_at,
                            exhausted=rejected and name == limit_type))
    if limit_type is not None and limit_type not in {row["name"] for row in rows}:
        rows.append(_window(limit_type, _percent(info.get("utilization")), top_reset, exhausted=rejected))
    if rejected and not any(row["exhausted"] for row in rows):
        rows.append(_window("limit", None, top_reset, exhausted=True))
    return rows


def _codex(limits: dict[str, Any], now: datetime) -> list[dict]:
    rows = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        resets_at = _reset(window.get("resets_at"))
        seconds = _number(window.get("resets_in_seconds"))
        if resets_at is None and seconds is not None and seconds >= 0:
            try:
                resets_at = (now.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()
            except (OverflowError, ValueError):
                resets_at = None
        rows.append(_window(_window_name(window.get("window_minutes")) or key,
                            _number(window.get("used_percent")), resets_at))
    return rows


def parse_usage(text: str, now: datetime) -> dict[str, list[dict]]:
    """The last usage report per executor in ``text`` (JSON lines), with exhaustion marks."""
    found: dict[str, list[dict]] = {}
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
        try:
            if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                executor, rows = "claude", _claude(event["rate_limit_info"])
            else:
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                limits = payload.get("rate_limits")
                if not isinstance(limits, dict):
                    continue
                executor, rows = "codex", _codex(limits, now)
        except (OverflowError, TypeError, ValueError):
            # Unreadable usage falls back to the configured wait; it never stops a run.
            continue
        if rows:
            found[executor] = rows
    return found


def latest_usage(text: str, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """The last readable usage per executor in the stored form."""
    found = {}
    for executor, rows in parse_usage(text, now or datetime.now(UTC)).items():
        stored = [{key: row[key] for key in ("name", "used_percent", "resets_at")}
                  for row in rows if row["used_percent"] is not None]
        if stored:
            found[executor] = stored
    return found


def record_from_log(store: Any, log: Path, offset: int = 0, now: datetime | None = None) -> dict[str, list[dict]]:
    """Record usage reported after ``offset`` in ``log`` and return the parsed windows.

    Unreadable logs are ignored.
    """
    try:
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    now = now or datetime.now(UTC)
    for executor, windows in latest_usage(text, now).items():
        store.record_usage(executor, windows)
    return parse_usage(text, now)


def limit_reset(windows: list[dict] | None, now: datetime, fallback: datetime | None = None) -> datetime | None:
    """Return when every exhausted window has reset.

    An exhausted window without a future reset has an unknown deadline, so the
    fallback also bounds the wait; without a fallback the result is unknown.
    """
    resets, unknown = [], False
    for window in windows or []:
        if not window.get("exhausted"):
            continue
        until = datetime.fromisoformat(window["resets_at"]) if window.get("resets_at") else None
        if until is None or until <= now:
            unknown = True
        else:
            resets.append(until)
    if unknown:
        if fallback is None:
            return None
        resets.append(fallback)
    return max(resets, default=fallback)

"""Read executor usage windows from Claude and Codex stream logs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any


def _timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _window(name: str, used_percent: float | None, resets_at: str | None, *, exhausted: bool = False) -> dict:
    exhausted = exhausted or (used_percent is not None and used_percent >= 100)
    return {"name": name, "used_percent": used_percent, "resets_at": resets_at, "exhausted": exhausted}


def _claude(info: dict) -> list[dict]:
    rejected = info.get("status") == "rejected"
    limit_type = info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None
    windows = []
    unified = info.get("unifiedWindows")
    for name, value in (unified.items() if isinstance(unified, dict) else ()):
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        utilization = _number(value.get("utilization"))
        resets_at = _timestamp(value.get("resetsAt"))
        if resets_at is None and name == limit_type:
            resets_at = _timestamp(info.get("resetsAt"))
        windows.append(_window(
            name, None if utilization is None else utilization * 100, resets_at,
            exhausted=rejected and name == limit_type,
        ))
    if limit_type is not None and limit_type not in {window["name"] for window in windows}:
        utilization = _number(info.get("utilization"))
        windows.append(_window(
            limit_type, None if utilization is None else utilization * 100, _timestamp(info.get("resetsAt")),
            exhausted=rejected,
        ))
    if rejected and not any(window["exhausted"] for window in windows) and _timestamp(info.get("resetsAt")):
        windows.append(_window("limit", None, _timestamp(info.get("resetsAt")), exhausted=True))
    return windows


def _window_name(minutes: Any) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0:
        return None
    minutes = int(minutes)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _codex(limits: dict, now: datetime) -> list[dict]:
    windows = []
    for slot in ("primary", "secondary"):
        value = limits.get(slot)
        if not isinstance(value, dict):
            continue
        name = _window_name(value.get("window_minutes")) or slot
        resets_at = _timestamp(value.get("resets_at"))
        seconds = _number(value.get("resets_in_seconds"))
        if resets_at is None and seconds is not None and seconds >= 0:
            try:
                resets_at = (now.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()
            except (OverflowError, ValueError):
                resets_at = None
        windows.append(_window(name, _number(value.get("used_percent")), resets_at))
    return windows


def read_usage(path: Path, now: datetime, *, offset: int = 0) -> dict[str, dict]:
    """Return the latest usage snapshot per executor written after ``offset``."""
    snapshots: dict[str, dict] = {}
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return snapshots
    with stream:
        stream.seek(offset)
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                    executor, windows = "claude", _claude(event["rate_limit_info"])
                else:
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                    limits = payload.get("rate_limits")
                    if not isinstance(limits, dict):
                        continue
                    executor, windows = "codex", _codex(limits, now)
            except (OverflowError, TypeError, ValueError):
                # Unreadable usage must fall back to the configured wait, never stop the run.
                continue
            if windows:
                snapshots[executor] = {"seen_at": now.astimezone(UTC).isoformat(), "windows": windows}
    return snapshots


def exhausted(snapshot: dict | None) -> bool:
    return any(window.get("exhausted") for window in (snapshot or {}).get("windows", []))


def limit_reset(snapshot: dict | None, now: datetime, fallback: datetime | None = None) -> datetime | None:
    """Return when every exhausted window has reset.

    An exhausted window without a future reset has an unknown deadline, so the
    fallback also bounds the wait; without a fallback the result is unknown.
    """
    resets, unknown = [], False
    for window in (snapshot or {}).get("windows", []):
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

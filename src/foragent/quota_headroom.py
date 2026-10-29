"""Quota headroom (#186): which of Claude and Codex has more of its plan left per hour.

Headroom is the minimum, over an executor's current windows, of (cap - used%) / hours to
the reset. Claude's cap is its keep_percent (100 under ``fa quota mode burn``); the other's
is 100. A window whose reset is unreadable is left out; a window that already reset is not
current. A current window at 100% or more is used up: a hard limit until its reset.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.config import Config

PAIR = ("claude", "codex")


def current_windows(usage: dict, name: str, now: datetime) -> list[tuple[float, datetime]]:
    """``name``'s windows whose readable reset is still ahead, as (used percent, reset)."""
    record = usage.get(name) if isinstance(usage, dict) else None
    windows = record.get("windows", []) if isinstance(record, dict) else []
    found = []
    for window in windows if isinstance(windows, list) else ():
        if not isinstance(window, dict):
            continue
        percent, reset = window.get("used_percent"), window.get("resets_at")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        try:
            at = datetime.fromisoformat(reset)
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=now.tzinfo)
        if at > now:
            found.append((float(percent), at))
    return found


def used_up_until(usage: dict, name: str, now: datetime) -> datetime | None:
    """The latest reset among ``name``'s current used-up windows; None when none is used up."""
    return max((at for percent, at in current_windows(usage, name, now) if percent >= 100), default=None)


def headroom(usage: dict, name: str, now: datetime, cap: float) -> float | None:
    """Percent left under ``cap`` per hour until reset, at the tightest window; None without current usage."""
    rates = [(cap - percent) / ((at - now).total_seconds() / 3600)
             for percent, at in current_windows(usage, name, now)]
    return min(rates, default=None)


def cap(config: Config, name: str, mode: str) -> float:
    if name == "claude" and mode != "burn":
        keep = config.claude_quota.get("keep_percent") if isinstance(config.claude_quota, dict) else None
        return float(keep) if isinstance(keep, (int, float)) else 100.0
    return 100.0


def order(config: Config, lane: str, usage: dict, now: datetime, mode: str) -> tuple[list[dict], dict] | None:
    """The headroom-sorted candidates and their rates, or None when #786's order applies.

    ``config`` is lane-adjusted; the original lane settings say whether the lane fixes its executor.
    An order the person wrote, a lane executor, or usage missing on either side leaves #786's order.
    """
    if getattr(config, "order_written", False) or (lane != "main" and config.lanes.get(lane, {}).get("executor")):
        return None
    if config.executor not in PAIR:
        return None
    rates = {name: headroom(usage, name, now, cap(config, name, mode)) for name in PAIR}
    if any(rate is None for rate in rates.values()):
        return None
    keep = config.claude_quota.get("keep_percent") if isinstance(config.claude_quota, dict) else None
    entries = {"claude": {"name": "claude", "quota": "subscription", "keep_percent": keep},
               "codex": {"name": "codex", "quota": "subscription"}}
    # Stable on ties: the lane's default executor stays first, so equal headroom never switches.
    names = sorted(PAIR, key=lambda name: (-rates[name], name != config.executor))
    return [scheduler_quota.billed(entries[name]) for name in names], rates


def switch(rates: dict, chosen: str, default: str):
    """The Switch a headroom-chosen executor records: from the lane's default to ``chosen``."""
    a, b = f"{rates[chosen]:.1f}", f"{rates[default]:.1f}"
    return scheduler_quota.Switch(f"headroom {a}/h > {b}/h", "switch_reason_headroom",
                                  {"chosen": a, "other": b}, default, chosen)


from foragent import scheduler_quota  # noqa: E402  # Call-time lookups; scheduler_quota imports this module.

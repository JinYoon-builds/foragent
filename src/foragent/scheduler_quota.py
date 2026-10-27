"""Claude plan share (#107): keep mode starts new runs with Codex once Claude passes keep_percent.

`fa quota mode` is the only writer of ``<state>/claude-quota.json``; the tick only reads it.
The tick writes a run's ``implementer`` once, when it starts the run.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from foragent.config import Config, resolved_reviewer

if TYPE_CHECKING:
    from foragent.state import StateStore


def quota_mode(store: StateStore, config: Config) -> tuple[str, str]:
    """The effective mode and its source: ``local`` (fa quota mode) or ``config``."""
    local = store.claude_quota_mode()
    return (local, "local") if local is not None else (config.claude_quota["mode"], "config")


def claude_percent(store: StateStore, now: datetime | None = None) -> float | None:
    """The highest Claude window whose reset is still ahead; None when none is known."""
    now = now or store._now()
    try:
        windows = store.usage().get("claude", {}).get("windows", [])
    except (OSError, ValueError, AttributeError):
        return None
    found = []
    for window in windows if isinstance(windows, list) else ():
        if not isinstance(window, dict):
            continue
        percent, reset = window.get("used_percent"), window.get("resets_at")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        try:
            if reset is not None and datetime.fromisoformat(reset) <= now:
                continue  # This window already reset; its share no longer counts.
        except (TypeError, ValueError):
            continue
        found.append(float(percent))
    return max(found, default=None)


def claude_allowed(store: StateStore, config: Config, now: datetime | None = None) -> bool:
    """False in keep mode while a current Claude window is at or above keep_percent."""
    if quota_mode(store, config)[0] == "burn":
        return True
    percent = claude_percent(store, now)
    return percent is None or percent < config.claude_quota["keep_percent"]


def switch_reason(store: StateStore, config: Config, now: datetime) -> str | None:
    """Why a new run on this lane-adjusted ``config`` starts with Codex instead of Claude, or None.

    Claude paused at its hard limit switches in keep and burn mode alike; keep mode
    also switches once a current Claude window reaches keep_percent.
    """
    if config.executor != "claude":
        return None
    if (until := store.executor_pause_until("claude", now)) is not None:
        return f"Claude paused at its limit until {until.isoformat()}"
    if claude_allowed(store, config, now):
        return None
    percent = claude_percent(store, now)
    return f"Claude keep share {percent:g}% >= {config.claude_quota['keep_percent']}%"


def switched_config(config: Config, implementer: str | None, claude_paused: bool = False) -> Config:
    """A lane-adjusted config with the run's stored implementer; None keeps the lane's executor.

    A reviewer pinned to ``none`` stays; otherwise Claude reviews the switched run
    (cross-model), and Codex only while Claude is paused at its hard limit.
    """
    from dataclasses import replace

    if implementer is None or implementer == config.executor:
        return config
    previous = resolved_reviewer(config)
    reviewer = "none" if previous == "none" else ("codex" if claude_paused else "claude")
    return replace(
        config, executor=implementer, executor_model=None, executor_fallback_model=None, reviewer=reviewer,
        reviewer_model=config.reviewer_model if reviewer == previous else None,
    )


def claude_caused(store: StateStore, config: Config, lane: str, now: datetime) -> bool:
    """A lane pause is Claude's when Claude is paused and the lane's configured pair includes Claude."""
    from foragent.config import lane_executors

    return store.executor_paused("claude", now) and "claude" in lane_executors(config, lane)


def lane_start(store: StateStore, config: Config, lane: str, now: datetime) -> tuple[Config, str | None]:
    """The config a new gate-started run on ``lane`` uses, and the switch reason when keep mode applies."""
    from foragent.run_implementation import lane_config

    adjusted = lane_config(config, lane)
    reason = switch_reason(store, adjusted, now)
    if reason is None:
        return adjusted, None
    return switched_config(adjusted, "codex", store.executor_paused("claude", now)), reason


def record_switch(store: StateStore, now: datetime, number: int, reason: str, language: str) -> str:
    """Journal one executor-switch event and return the tick report line."""
    from foragent.i18n import text
    from foragent.orch_events import append_event

    append_event(store, now, number, "executor-switch", f"#{number} implementer claude -> codex: {reason}",
                 **{"from": "claude", "to": "codex", "reason": reason})
    return text("executor_switch", language, number=number, reason=reason)

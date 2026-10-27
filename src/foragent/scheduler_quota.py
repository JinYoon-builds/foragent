"""Claude plan share (#107): keep mode starts new runs with Codex once Claude passes keep_percent;
Claude paused at its hard limit moves every Claude role of a new run to Codex in either mode.

`fa quota mode` is the only writer of ``<state>/claude-quota.json``; the tick only reads it.
The tick writes a run's ``implementer`` once, when it starts the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

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


@dataclass(frozen=True)
class Switch:
    """Why one start uses Codex instead of Claude.

    ``reason`` is the fixed English machine reason kept in the event; ``key`` and
    ``values`` render it for a person through the messages registry.
    """

    reason: str
    key: str
    values: dict


@dataclass(frozen=True)
class Snapshot:
    """Claude's pause, share and mode, each read once for one start decision."""

    claude_until: datetime | None
    percent: float | None
    mode: str

    @classmethod
    def read(cls, store: StateStore, config: Config, now: datetime) -> Snapshot:
        return cls(store.executor_pause_until("claude", now), claude_percent(store, now), quota_mode(store, config)[0])

    @property
    def claude_paused(self) -> bool:
        return self.claude_until is not None

    def paused(self, store: StateStore, name: str, now: datetime) -> bool:
        return self.claude_paused if name == "claude" else store.executor_paused(name, now)


def switch_for(config: Config, snap: Snapshot) -> Switch | None:
    """The switch a start on this lane-adjusted ``config`` takes, from one snapshot.

    Claude paused at its hard limit switches in keep and burn mode alike; keep mode
    also switches once a current Claude window reaches keep_percent.
    """
    if config.executor != "claude":
        return None
    if snap.claude_until is not None:
        until = snap.claude_until.isoformat()
        return Switch(f"Claude paused at its limit until {until}", "switch_reason_limit", {"until": until})
    keep = config.claude_quota["keep_percent"]
    if snap.mode == "burn" or snap.percent is None or snap.percent < keep:
        return None
    percent = f"{snap.percent:g}"
    return Switch(f"Claude keep share {percent}% >= {keep}%", "switch_reason_keep", {"percent": percent, "keep": keep})


def switched_config(config: Config, implementer: str | None, claude_paused: bool = False) -> Config:
    """A lane-adjusted config with the run's stored implementer and Claude's hard-limit fallback.

    ``implementer`` None keeps the lane's executor. A switched run's reviewer is Claude
    (cross-model) unless it is pinned to ``none``. While Claude is paused at its hard
    limit, every role Claude would fill moves to Codex: the implementer through the
    stored choice, the reviewer here, whichever executor the lane configures.
    """
    previous = resolved_reviewer(config)
    executor = implementer if implementer is not None else config.executor
    reviewer = previous if executor == config.executor else ("none" if previous == "none" else "claude")
    if claude_paused and reviewer == "claude":
        reviewer = "codex"
    if executor == config.executor and reviewer == previous:
        return config
    same = executor == config.executor
    return replace(
        config, executor=executor, executor_model=config.executor_model if same else None,
        executor_fallback_model=config.executor_fallback_model if same else None, reviewer=reviewer,
        reviewer_model=config.reviewer_model if reviewer == previous else None,
    )


# A new run record after one of these continues the same run (as ``restarts`` counts it).
CONTINUED_OUTCOMES = frozenset({"stalled-restart", "crashed", "limit-wait"})


def kept_implementer(store: StateStore, number: int) -> str | None:
    """The implementer a continued run keeps; None when the next start chooses afresh."""
    previous = store.get_run(number)
    return previous.implementer if previous is not None and previous.outcome in CONTINUED_OUTCOMES else None


class Start(NamedTuple):
    """One start decision: the run's config, stored implementer, switch, and whether its pair waits."""

    config: Config
    implementer: str | None
    switch: Switch | None
    held: bool


def decide(store: StateStore, config: Config, lane: str, now: datetime, number: int | None = None) -> Start:
    """Decide a gate start from one snapshot; ``number`` lets a continued run keep its implementer."""
    from foragent.run_implementation import lane_config

    snap = Snapshot.read(store, config, now)
    adjusted = lane_config(config, lane)
    kept = kept_implementer(store, number) if number is not None else None
    switch = None if kept is not None else switch_for(adjusted, snap)
    implementer = kept if kept is not None else ("codex" if switch is not None else None)
    run_config = switched_config(adjusted, implementer, snap.claude_paused)
    held = any(snap.paused(store, name, now) for name in {run_config.executor, resolved_reviewer(run_config)})
    if not held and store.lane_paused(lane, now):
        # A lane pause mirrors a limit of the lane's configured pair. A Claude-caused one does
        # not hold the start: every Claude role of this run already moved to Codex, which is free.
        # The pause itself stays in place.
        from foragent.config import lane_executors

        claude_caused = snap.claude_paused and "claude" in lane_executors(config, lane)
        held = not (claude_caused and "claude" not in {run_config.executor, resolved_reviewer(run_config)})
    return Start(run_config, implementer, switch, held)


def run_start(store: StateStore, config: Config, lane: str, number: int,
              now: datetime) -> tuple[Config, str | None, Switch | None]:
    """The config, stored implementer and switch for a gate start of ``number``.

    A continued run keeps its stored implementer and records no new switch.
    """
    start = decide(store, config, lane, now, number)
    return start.config, start.implementer, start.switch


def switch_journal(store: StateStore, now: datetime, number: int, switch: Switch) -> Callable[[], None]:
    """Write the run's executor-switch event; ``start_run`` calls it under the lock that stores the run."""
    from foragent.orch_events import append_event

    return lambda: append_event(store, now, number, "executor-switch",
                                f"#{number} implementer claude -> codex: {switch.reason}", locked=True,
                                **{"from": "claude", "to": "codex", "reason": switch.reason})


def switch_line(number: int, switch: Switch, language: str) -> str:
    """The tick report line for a switch, in the repository's language."""
    from foragent.i18n import text

    return text("executor_switch", language, number=number, reason=text(switch.key, language, **switch.values))

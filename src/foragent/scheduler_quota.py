"""Executor admission (#107, #786): a start takes the first executor in the configured order
that can run. keep_percent moves on at a share, a hard limit moves on unless a spend_cap
allows credits, and an api executor stops at its daily spend_cap. ``fa quota mode burn``
turns off Claude's keep_percent only.

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
    """The effective mode and its source: ``local`` (fa quota mode) or ``config``.

    A repository executors list ignores claude_quota; there keep_percent alone decides.
    """
    from foragent.config import executors_explicit

    local = store.claude_quota_mode()
    if local is not None:
        return local, "local"
    return ("keep" if executors_explicit(config) else config.claude_quota["mode"]), "config"


def _usage(store: StateStore) -> dict:
    try:
        usage = store.usage()
    except (OSError, ValueError, AttributeError):
        return {}
    return usage if isinstance(usage, dict) else {}


def _percent(usage: dict, name: str, now: datetime) -> float | None:
    """The highest window of ``name`` whose reset is still ahead; None when none is known."""
    record = usage.get(name)
    windows = record.get("windows", []) if isinstance(record, dict) else []
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


def claude_percent(store: StateStore, now: datetime | None = None) -> float | None:
    """The highest Claude window whose reset is still ahead; None when none is known."""
    return _percent(_usage(store), "claude", now or store._now())


def claude_allowed(store: StateStore, config: Config, now: datetime | None = None) -> bool:
    """False in keep mode while a current Claude window is at or above keep_percent."""
    if quota_mode(store, config)[0] == "burn":
        return True
    percent = claude_percent(store, now)
    return percent is None or percent < config.claude_quota["keep_percent"]


def label(name: str) -> str:
    return {"claude": "Claude", "codex": "Codex"}.get(name, name)


@dataclass(frozen=True)
class Switch:
    """Why one start uses ``target`` instead of ``source``.

    ``reason`` is the fixed English machine reason kept in the event; ``key`` and
    ``values`` render it for a person through the messages registry.
    """

    reason: str
    key: str
    values: dict
    source: str = "claude"
    target: str = "codex"


# Only these executors' runs report a cost, so only they can spend through an api quota.
MEASURED = frozenset({"claude"})


def fallback_order(config: Config) -> list[dict]:
    """The lane-adjusted executor first, then the configured order, wrapping around (#786).

    A lane executor missing from the list uses only that executor, as before the list existed.
    """
    entries = [dict(entry) for entry in config.executors]
    names = [entry["name"] for entry in entries]
    if config.executor not in names:
        return [{"name": config.executor, "quota": "subscription"}]
    index = names.index(config.executor)
    return entries[index:] + entries[:index]


def blocked(entry: dict, store: StateStore, now: datetime, usage: dict, mode: str) -> tuple[Switch | None, bool]:
    """Why ``entry`` cannot implement now (None when it can), and whether a hard limit without a cap holds it."""
    name, quota, cap = entry["name"], entry.get("quota", "subscription"), entry.get("spend_cap")
    until = store.executor_pause_until(name, now)
    limit = None if until is None else Switch(
        f"{label(name)} paused at its limit until {until.isoformat()}", "switch_reason_limit",
        {"name": label(name), "until": until.isoformat()}, name)
    spent = store.spend_today(name, now) if cap is not None else 0.0
    capped = None if cap is None or spent < cap else Switch(
        f"{label(name)} spent {spent:g} of its daily cap {cap:g}", "switch_reason_cap",
        {"name": label(name), "spent": f"{spent:g}", "cap": f"{cap:g}"}, name)
    if quota == "api":
        if name not in MEASURED:  # A cap that is never measured cannot hold spending back.
            return Switch(f"{label(name)} reports no cost for its api cap", "switch_reason_unmeasured",
                          {"name": label(name)}, name), False
        return capped or limit, False
    if quota == "unlimited":
        return limit, False
    if limit is not None:  # Past the subscription limit only credits under a cap can run.
        return (limit, True) if cap is None else (capped, False)
    keep = None if name == "claude" and mode == "burn" else entry.get("keep_percent")
    percent = _percent(usage, name, now)
    if keep is None or percent is None or percent < keep:
        return None, False
    shown = f"{percent:g}"
    return Switch(f"{label(name)} keep share {shown}% >= {keep}%", "switch_reason_keep",
                  {"name": label(name), "percent": shown, "keep": keep}, name), False


def switched_config(config: Config, implementer: str | None, claude_paused: bool = False) -> Config:
    """A lane-adjusted config with the run's stored implementer and Claude's hard-limit fallback.

    ``implementer`` None keeps the lane's executor. A run keeps its configured reviewer
    unless that reviewer is the executor it fell back to; then the executor it fell back
    from reviews, when that is Claude or Codex. While Claude is paused at its hard limit,
    a Claude reviewer moves to Codex.
    """
    previous = resolved_reviewer(config)
    executor = implementer if implementer is not None else config.executor
    reviewer = previous
    if executor != config.executor and previous == executor and config.executor in ("claude", "codex"):
        reviewer = config.executor
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
    """Decide a gate start from one usage read; ``number`` lets a continued run keep its implementer.

    The first executor in the lane's fallback order that can run implements. When none can,
    the start waits, and each subscription executor whose hard limit (without a spend_cap)
    holds it records one credits question, once ever.
    """
    from foragent.config import lane_executors
    from foragent.run_implementation import lane_config

    adjusted = lane_config(config, lane)
    usage, mode = _usage(store), quota_mode(store, config)[0]
    order = fallback_order(adjusted)
    kept = kept_implementer(store, number) if number is not None else None
    if kept is not None:
        order = [next((entry for entry in order if entry["name"] == kept), {"name": kept, "quota": "subscription"})]
    switch, chosen, asks = None, None, []
    for entry in order:
        why, ask = blocked(entry, store, now, usage, mode)
        if why is None:
            chosen = entry["name"]
            break
        switch = switch or why
        if ask:
            asks.append(entry["name"])
    if chosen is None:
        for name in asks:
            _ask_credits(store, config, name)
        return Start(adjusted, kept, None, True)
    implementer = kept if kept is not None else (None if chosen == adjusted.executor else chosen)
    switch = replace(switch, target=chosen) if kept is None and implementer is not None else None
    run_config = switched_config(adjusted, implementer, store.executor_paused("claude", now))
    held = store.executor_paused(resolved_reviewer(run_config), now)
    if not held and store.lane_paused(lane, now):
        # A lane pause mirrors a limit of the lane's configured pair. One caused by an executor
        # this run no longer needs does not hold it; the pause itself stays in place.
        held = not any(store.executor_paused(name, now) for name in lane_executors(config, lane))
    return Start(run_config, implementer, switch, held)


def _ask_credits(store: StateStore, config: Config, name: str) -> None:
    """The one credits question for ``name``; the person's answer only records their choice."""
    from foragent.i18n import text
    from foragent.orch_inbox import ask_once

    language = config.language
    ask_once(store, f"credits:{name}", text("credits_question", language, name=label(name)),
             [text("credits_wait", language), text("credits_allow", language)], 1)


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
                                f"#{number} implementer {switch.source} -> {switch.target}: {switch.reason}",
                                locked=True, **{"from": switch.source, "to": switch.target, "reason": switch.reason})


def switch_line(number: int, switch: Switch, language: str) -> str:
    """The tick report line for a switch, in the repository's language."""
    from foragent.i18n import text

    reason = text(switch.key, language, **{"name": label(switch.source), **switch.values})
    return text("executor_switch", language, number=number, source=label(switch.source),
                target=label(switch.target), reason=reason)


def spend_counts(store: StateStore, config: Config, name: str, started_at: str | None) -> bool:
    """Whether a run's cost on ``name`` is money: an api executor, or credits on a
    subscription executor started past its hard-limit pause under a spend_cap."""
    entry = next((entry for entry in config.executors if entry["name"] == name), None)
    if entry is None or entry.get("spend_cap") is None:
        return False
    if entry.get("quota") == "api":
        return True
    try:
        started = datetime.fromisoformat(started_at) if started_at else None
    except ValueError:
        return False
    return started is not None and store.executor_paused(name, started)


def record_cost(store: StateStore, config: Config, started_at: str | None, lines: list[str]) -> float | None:
    """Record the Claude result's ``total_cost_usd`` from a session's log lines when it is money."""
    import json

    cost = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        value = event.get("total_cost_usd") if isinstance(event, dict) and event.get("type") == "result" else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cost = (cost or 0.0) + float(value)
    if cost is None or not spend_counts(store, config, "claude", started_at):
        return None
    store.record_spend("claude", cost)
    return cost

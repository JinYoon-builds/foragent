"""Executor admission (#107, #786): a start takes the first executor in the configured order
that can run. keep_percent moves on at a share, a hard limit moves on unless a spend_cap
allows credits, and an api executor stops at its daily spend_cap. ``fa quota mode burn``
turns off Claude's keep_percent only.

`fa quota mode` is the only writer of ``<state>/claude-quota.json``; the tick only reads it.
The tick writes a run's ``executor`` and ``reviewer`` once per start (#186).
Without a written order, Claude and Codex are tried in headroom order (quota_headroom).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import os
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


def api_key_billing() -> bool:
    """Whether Claude runs started by this process bill the API (#889): derived from the
    gate's own environment, never stored. An OAuth token keeps Claude on its subscription."""
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def billed(entry: dict) -> dict:
    """``entry`` as it bills now: under an API key Claude is api, capped only by its own spend_cap."""
    if entry["name"] == "claude" and api_key_billing():
        return {**entry, "quota": "api"}
    return entry


def fallback_order(config: Config) -> list[dict]:
    """The lane-adjusted executor first, then the configured order, wrapping around (#786).

    A lane executor missing from the list uses only that executor, as before the list existed.
    """
    entries = [billed(dict(entry)) for entry in config.executors]
    names = [entry["name"] for entry in entries]
    if config.executor not in names:
        return [billed({"name": config.executor, "quota": "subscription"})]
    index = names.index(config.executor)
    return entries[index:] + entries[:index]


def entry_for(config: Config, name: str) -> dict:
    """``name``'s executors entry as it bills now; an unlisted executor counts as a plain subscription."""
    return billed(next((dict(entry) for entry in config.executors if entry["name"] == name),
                       {"name": name, "quota": "subscription"}))


def blocked(entry: dict, store: StateStore, now: datetime, usage: dict, mode: str,
            keep: bool = True) -> tuple[Switch | None, bool]:
    """Why ``entry`` cannot run now (None when it can), and whether a hard limit without a cap holds it.

    A reached daily spend_cap counts as a pause until the next UTC day, whatever else is paused.
    ``keep=False`` skips keep_percent, which only moves implementers.
    """
    name, quota, cap = entry["name"], entry.get("quota", "subscription"), entry.get("spend_cap")
    if quota == "api" and name not in MEASURED:  # A cap that is never measured cannot hold spending back.
        return Switch(f"{label(name)} reports no cost, so its api spend cannot be measured against spend_cap",
                      "switch_reason_unmeasured", {"name": label(name)}, name), False
    if quota == "api" and cap is None:  # Only an API key makes this (#889): no spend without a cap.
        return Switch(f"{label(name)} bills the API through ANTHROPIC_API_KEY and has no spend_cap; "
                      "set a spend_cap in its executors entry to allow it",
                      "switch_reason_uncapped", {"name": label(name)}, name), False
    spent = store.spend_today(name, now) if cap is not None else 0.0
    if cap is not None and spent >= cap:
        return Switch(f"{label(name)} spent {spent:g} of its daily cap {cap:g}", "switch_reason_cap",
                      {"name": label(name), "spent": f"{spent:g}", "cap": f"{cap:g}"}, name), False
    until = store.executor_pause_until(name, now)
    if quota == "subscription" and (used := quota_headroom.used_up_until(usage, name, now)) is not None:
        until = max(until, used) if until is not None else used  # A used-up window is a hard limit (#186).
    if until is not None and not (quota == "subscription" and cap is not None):
        # Past a subscription limit only credits under a cap run; others wait for the reset.
        return Switch(f"{label(name)} paused at its limit until {until.isoformat()}", "switch_reason_limit",
                      {"name": label(name), "until": until.isoformat()}, name), quota == "subscription"
    if until is not None or quota != "subscription" or not keep:
        return None, False
    percent = _percent(usage, name, now)
    share = None if name == "claude" and mode == "burn" else entry.get("keep_percent")
    if share is None or percent is None or percent < share:
        return None, False
    shown = f"{percent:g}"
    return Switch(f"{label(name)} keep share {shown}% >= {share}%", "switch_reason_keep",
                  {"name": label(name), "percent": shown, "keep": share}, name), False


def credits(store: StateStore, config: Config, name: str, now: datetime) -> bool:
    """Whether a session of ``name`` launched now spends money: an api executor, or
    credits past a subscription hard-limit pause under a spend_cap."""
    entry = entry_for(config, name)
    if entry.get("spend_cap") is None:
        return False
    return entry.get("quota") == "api" or store.executor_paused(name, now) or (
        quota_headroom.used_up_until(_usage(store), name, now) is not None)


def hard_limited(store: StateStore, config: Config, name: str, now: datetime, usage: dict | None = None) -> bool:
    """Whether ``name`` is at a hard limit now: a pause or a used-up window (#186), not a cap."""
    usage = _usage(store) if usage is None else usage
    why = blocked(entry_for(config, name), store, now, usage, "keep", keep=False)[0]
    return why is not None and why.key == "switch_reason_limit"


# A new run record after one of these continues the same run (as ``restarts`` counts it).
CONTINUED_OUTCOMES = frozenset({"stalled-restart", "crashed", "limit-wait"})


def kept_implementer(store: StateStore, number: int, lane_executor: str) -> str | None:
    """The executor a continued run keeps; None when the next start chooses afresh.

    A record from before #186 stored only ``implementer``; None there meant ``lane_executor``.
    """
    previous = store.get_run(number)
    if previous is None or previous.outcome not in CONTINUED_OUTCOMES:
        return None
    return getattr(previous, "executor", None) or previous.implementer or lane_executor


class Start(NamedTuple):
    """One start decision: the run's config, stored implementer, switch, and whether its pair waits."""

    config: Config
    implementer: str | None
    switch: Switch | None
    held: bool
    reason: str | None = None  # Why the start waits, in the executor-wait words; None when it starts.


def decide(store: StateStore, config: Config, lane: str, now: datetime, number: int | None = None) -> Start:
    """Decide a gate start from one usage read; ``number`` lets a continued run keep its implementer.

    The first executor in the lane's fallback order that can run implements. When none can,
    the start waits with the first executor's reason, and each subscription executor whose
    hard limit (without a spend_cap) holds it records one credits question, once ever.
    """
    from foragent.config import lane_executors
    from foragent.run_implementation import lane_config

    store = quota_view.view(store, now)  # Every question below reads one snapshot of the quota facts.
    adjusted = lane_config(config, lane)
    usage, mode = _usage(store), quota_mode(store, config)[0]
    ranked = quota_headroom.order(adjusted, lane, usage, now, mode)
    order = ranked[0] if ranked is not None else fallback_order(adjusted)
    kept = kept_implementer(store, number, adjusted.executor) if number is not None else None
    switch, released = None, False
    if kept is not None:
        kept_entry = next((entry for entry in order if entry["name"] == kept), entry_for(adjusted, kept))  # Billed as listed.
        why = blocked(kept_entry, store, now, usage, mode, keep=False)[0]
        if why is not None and why.key == "switch_reason_limit":
            # A kept executor at a hard limit (a pause or a used-up window, #186) is released and the
            # next one takes over with a summary; the release is always journaled as a limit switch.
            switch, kept, released = why, None, True
        else:
            order = [kept_entry]
    chosen, asks = None, []
    for entry in order:
        # A continued run (crashed, stalled-restart, limit-wait) keeps its implementer: only a hard
        # limit, cap or unmeasured spend holds it, never keep_percent, as #107 continuations did.
        why, ask = blocked(entry, store, now, usage, mode, keep=kept is None)
        if why is None:
            chosen = entry["name"]
            break
        switch = switch or why
        if ask:
            asks.append(entry["name"])
    if chosen is None:
        for name in asks:
            _ask_credits(store, config, name)
        return Start(adjusted, kept, None, True, switch.reason)
    implementer = None if chosen == adjusted.executor else chosen  # None: the lane default.
    if kept is None and implementer is not None and switch is None and ranked is not None:
        switch = quota_headroom.switch(ranked[1], chosen, adjusted.executor)
    switch = replace(switch, target=chosen) if kept is None and switch is not None and (
        implementer is not None or released) else None
    chosen_config = run_config(store, adjusted, implementer, now)
    reason = None
    reviewer = resolved_reviewer(chosen_config)
    if chosen_config.executor == adjusted.executor and reviewer != "none":
        # Today's rule: a run on its own executor waits for its reviewer; a fallback never does.
        why, ask = blocked(entry_for(adjusted, reviewer), store, now, usage, mode, keep=False)
        reason = why and why.reason
        if ask:  # The reviewer's hard limit holds this start: its one credits question.
            _ask_credits(store, config, reviewer)
    if reason is None and store.lane_paused(lane, now) and not any(
            store.executor_paused(name, now) for name in lane_executors(config, lane)):
        # A lane pause mirrors a limit of the lane's configured pair. One caused by an executor
        # this run no longer needs does not hold it; the pause itself stays in place.
        reason = f"lane {lane} is paused"
    return Start(chosen_config, implementer, switch, reason is not None, reason)


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


# Split out to stay under the module line limit; re-exported so callers keep their imports.
from foragent import quota_headroom, quota_view  # noqa: E402
from foragent.quota_reviewer import can_review, choose_reviewer, pair_config, run_config, switched_config  # noqa: E402,F401
from foragent.quota_spend import record_cost  # noqa: E402,F401
from foragent.quota_events import choose_executor, start_journal, switch_journal, switch_line  # noqa: E402,F401

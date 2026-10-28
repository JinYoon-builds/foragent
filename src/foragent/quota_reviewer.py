"""Who reviews a run that starts on its own executor or falls back to another (#107, #786)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from foragent.config import Config, resolved_reviewer

if TYPE_CHECKING:
    from foragent.state import StateStore


def can_review(config: Config, name: str) -> bool:
    """Unlimited and unknown executors never review."""
    return name in ("claude", "codex") and scheduler_quota.entry_for(config, name).get("quota") != "unlimited"


def choose_reviewer(config: Config, executor: str, claude_paused: bool,
                    available: Callable[[str], bool]) -> str:
    """The reviewer of a run of lane-adjusted ``config`` on ``executor``; the one place it is chosen.

    A run on its own executor keeps the configured reviewer, which moves from Claude to Codex
    while Claude is paused at its hard limit. A fallback never waits for a reviewer. One between
    Claude and Codex is reviewed by the executor it fell back from when that is free, else by
    the configured reviewer when free and not the run's executor, else by the run's own
    executor. Any other fallback keeps the configured reviewer when it is free, else the run's
    own executor reviews. Unlimited and unknown executors never review; with no candidate
    left: ``none``.
    """
    previous = resolved_reviewer(config)
    source = config.executor
    if previous == "none":
        return "none"
    if executor != source and {source, executor} <= {"claude", "codex"}:
        chain = [source, *([previous] if previous != executor else []), executor]
        return next((name for name in chain if can_review(config, name) and
                     (name == executor or available(name))), "none")
    if executor != source:
        # Any other fallback keeps the configured reviewer while it is free; it never waits for it.
        return next((name for name in (previous, executor) if can_review(config, name) and
                     (name == executor or available(name))), "none")
    kept = "codex" if claude_paused and previous == "claude" else previous
    return next((name for name in (kept, executor) if can_review(config, name)), "none")


def switched_config(config: Config, implementer: str | None, claude_paused: bool = False,
                    available: Callable[[str], bool] | None = None) -> Config:
    """A lane-adjusted config with the run's stored implementer and its reviewer (``choose_reviewer``).

    ``implementer`` None keeps the lane's executor; ``available`` says which executors can run now.
    """
    available = available or (lambda name: not (claude_paused and name == "claude"))
    previous = resolved_reviewer(config)
    executor = implementer if implementer is not None else config.executor
    reviewer = choose_reviewer(config, executor, claude_paused, available)
    if executor == config.executor and reviewer == previous:
        return config
    same = executor == config.executor
    return replace(
        config, executor=executor, executor_model=config.executor_model if same else None,
        executor_fallback_model=config.executor_fallback_model if same else None, reviewer=reviewer,
        reviewer_model=config.reviewer_model if reviewer == previous else None,
    )


def run_config(store: StateStore, config: Config, implementer: str | None, now: datetime) -> Config:
    """The runner's config for a lane-adjusted ``config``, choosing the reviewer as ``decide`` does."""
    def available(name: str) -> bool:
        entry = scheduler_quota.entry_for(config, name)
        return scheduler_quota.blocked(entry, store, now, {}, "keep", keep=False)[0] is None

    return switched_config(config, implementer, store.executor_paused("claude", now), available)


from foragent import scheduler_quota  # noqa: E402  # Call-time lookups; scheduler_quota imports this module.

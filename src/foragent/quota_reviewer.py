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
    """Unlimited, unknown and uncapped api executors (Claude on an API key, #889) never review."""
    entry = scheduler_quota.entry_for(config, name)
    quota = entry.get("quota")
    return name in ("claude", "codex") and quota != "unlimited" and not (
        quota == "api" and entry.get("spend_cap") is None)


def choose_reviewer(config: Config, executor: str, limited: Callable[[str], bool],
                    available: Callable[[str], bool]) -> str:
    """The reviewer of a run of lane-adjusted ``config`` on ``executor``; the one place it is chosen.

    A run on its own executor keeps the configured reviewer; one that is hard-limited moves to
    the run's own executor (#186), so the review never waits on a used-up side. A fallback never
    waits for a reviewer. One between Claude and Codex is reviewed by the executor it fell back
    from when that is free, else by the configured reviewer when free and not the run's
    executor, else by the run's own executor. Any other fallback keeps the configured reviewer
    when it is free, else the run's own executor reviews. Unlimited and unknown executors never
    review; with no candidate left: ``none``.
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
    kept = executor if limited(previous) else previous
    return next((name for name in (kept, executor) if can_review(config, name)), "none")


def switched_config(config: Config, implementer: str | None, limited: Callable[[str], bool] | None = None,
                    available: Callable[[str], bool] | None = None) -> Config:
    """A lane-adjusted config with the run's implementer and its reviewer (``choose_reviewer``).

    ``implementer`` None keeps the lane's executor; ``limited`` says which executors are at a
    hard limit, ``available`` which can run now (default: those not limited).
    """
    limited = limited or (lambda name: False)
    available = available or (lambda name: not limited(name))
    executor = implementer if implementer is not None else config.executor
    return pair_config(config, executor, choose_reviewer(config, executor, limited, available))


def pair_config(config: Config, executor: str, reviewer: str | None) -> Config:
    """Lane-adjusted ``config`` running the concrete pair; a reviewer None is ``none`` (no review)."""
    previous = resolved_reviewer(config)
    reviewer = reviewer or "none"
    if executor == config.executor and reviewer == previous:
        return config
    same = executor == config.executor
    return replace(
        config, executor=executor, executor_model=config.executor_model if same else None,
        executor_fallback_model=config.executor_fallback_model if same else None, reviewer=reviewer,
        reviewer_model=config.reviewer_model if reviewer == previous else None,
    )


def run_config(store: StateStore, config: Config, implementer: str | None, now: datetime) -> Config:
    """The run's config for a lane-adjusted ``config``, choosing the reviewer as ``decide`` does.

    It reads the same recorded usage as ``decide``, so a used-up window counts on both sides.
    """
    usage = scheduler_quota._usage(store)

    def available(name: str) -> bool:
        entry = scheduler_quota.entry_for(config, name)
        return scheduler_quota.blocked(entry, store, now, usage, "keep", keep=False)[0] is None

    return switched_config(config, implementer,
                           lambda name: scheduler_quota.hard_limited(store, config, name, now, usage), available)


def stored_config(store: StateStore, config: Config, run, now: datetime) -> Config:
    """The runner's config from the pair the tick stored (#186); it never chooses the executor again.

    A record from before #186 has only ``implementer`` and is read the old way. A stored reviewer
    that is hard-limited when a review launches moves then (``review_moved``), never here.
    """
    executor = getattr(run, "executor", None)
    if executor is None:
        return legacy_config(store, config, run.implementer, now)
    return pair_config(config, executor, run.reviewer)


def review_moved(n: int, ctx):
    """``ctx`` for a review launching now: a reviewer at a hard limit moves to the run's own executor.

    The review never waits for it (#186). The move is journaled as a same-model review (reason
    limit) once per run: a later round of the same run finds the event and adds none.
    """
    from foragent.orch_events import append_event, has_event

    config, now = ctx.config, ctx.now()
    reviewer, executor = resolved_reviewer(config), config.executor
    run = ctx.store.get_run(n)
    if getattr(run, "executor", None) is None:
        return ctx  # A record from before #186 keeps the old derivation (legacy_config).
    if reviewer in ("none", executor) or not scheduler_quota.hard_limited(ctx.store, config, reviewer, now):
        return ctx
    moved = executor if can_review(config, executor) else None
    if moved is not None and not has_event(ctx.store, n, "same-model-review", run=run.started_at):
        append_event(ctx.store, now, n, "same-model-review", f"#{n} {executor} reviews its own work: limit",
                     executor=executor, reason="limit", run=run.started_at)
    return replace(ctx, config=pair_config(config, executor, moved))


def reviewer_limited(n: int, ctx, name: str) -> bool:
    """Whether a review session of ``name`` must move instead of launching (``review_moved``)."""
    if name == ctx.config.executor or getattr(ctx.store.get_run(n), "executor", None) is None:
        return False
    return scheduler_quota.hard_limited(ctx.store, ctx.config, name, ctx.now())


def moving_review(review):
    """Run ``review`` with the reviewer it may use now; one that reaches its limit during the
    review's preparation (``ReviewerLimited`` at launch) reruns once on the run's own executor."""
    from functools import wraps

    from foragent.run_attempt import ReviewerLimited

    @wraps(review)
    def run(n: int, ctx, *args, **kwargs):
        try:
            return review(n, review_moved(n, ctx), *args, **kwargs)
        except ReviewerLimited:
            return review(n, review_moved(n, ctx), *args, **kwargs)

    return run


def legacy_config(store: StateStore, config: Config, implementer: str | None, now: datetime) -> Config:
    """The pre-#186 derivation for a record with only ``implementer``: only a Claude pause moves
    its review, and availability reads executor pauses alone, as those runs were started."""
    claude_paused = store.executor_paused("claude", now)

    def available(name: str) -> bool:
        entry = scheduler_quota.entry_for(config, name)
        return scheduler_quota.blocked(entry, store, now, {}, "keep", keep=False)[0] is None

    return switched_config(config, implementer, lambda name: claude_paused and name == "claude", available)


from foragent import scheduler_quota  # noqa: E402  # Call-time lookups; scheduler_quota imports this module.

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
    the run's own executor when that can review (``moves_to_worker``, #186); otherwise it stays and
    the start waits for it, so a quota limit never removes the review. A fallback never
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
    kept = executor if moves_to_worker(config, previous, executor, limited) else previous
    return next((name for name in (kept, executor) if can_review(config, name)), "none")


def moves_to_worker(config: Config, reviewer: str, executor: str, limited: Callable[[str], bool]) -> bool:
    """Whether a hard-limited ``reviewer`` moves to the run's own ``executor``; the one rule for the
    start, the review launch and the session launch. A worker that cannot review (unlimited or
    unknown) takes nothing over: the reviewer stays, and the run waits for its reset."""
    return reviewer not in ("none", executor) and can_review(config, executor) and limited(reviewer)


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
    store = quota_view.view(store, now)  # The same snapshot as the caller's decision, when it has one.
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
    paired = (legacy_config(store, config, run.implementer, now) if executor is None
              else pair_config(config, executor, run.reviewer))
    return assigned_model(paired, getattr(run, "executor_model", None))


def assigned_model(config: Config, model: str | None) -> Config:
    """``config`` running an issue assignment's model (D80); None leaves it unchanged.

    The assignment is the person's or orchestrator's explicit choice, so no configured fallback
    model replaces it during recovery (transient errors): the run keeps the assigned model.
    """
    if model is None:
        return config
    return replace(config, executor_model=model, executor_fallback_model=None)


def review_moved(n: int, ctx):
    """``ctx`` for a review launching now: a reviewer at a hard limit moves to the run's own executor.

    The review never waits for it (#186). The move is journaled as a same-model review (reason
    limit) once per run: a later round of the same run finds the event and adds none.
    """
    config, now = ctx.config, ctx.now()
    reviewer, executor = resolved_reviewer(config), config.executor
    run = ctx.store.get_run(n)
    if getattr(run, "executor", None) is None:
        return ctx  # A record from before #186 keeps the old derivation (legacy_config).
    if not moves_to_worker(config, reviewer, executor,
                           lambda name: scheduler_quota.hard_limited(ctx.store, config, name, now)):
        return ctx  # Free, or no worker can take it over: the launch hold then waits for the reset.
    return _moved(n, ctx)


def _moved(n: int, ctx):
    """``ctx`` with the review on the run's own executor, journaled once per run as a same-model review."""
    from foragent.orch_events import append_event, has_event

    executor, run = ctx.config.executor, ctx.store.get_run(n)
    if not has_event(ctx.store, n, "same-model-review", run=run.started_at):
        append_event(ctx.store, ctx.now(), n, "same-model-review", f"#{n} {executor} reviews its own work: limit",
                     executor=executor, reason="limit", run=run.started_at)
    return replace(ctx, config=pair_config(ctx.config, executor, executor))


def reviewer_limited(n: int, ctx, name: str, view) -> bool:
    """Whether a review session of ``name`` must move instead of launching (``review_moved``).

    ``view`` is the launch's one quota snapshot, which the launch hold also reads.
    """
    if getattr(ctx.store.get_run(n), "executor", None) is None:
        return False
    return moves_to_worker(ctx.config, name, ctx.config.executor,
                           lambda other: scheduler_quota.hard_limited(view, ctx.config, other, view.now))


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
            # The launch decided from its snapshot that moves_to_worker holds; the rerun honors that
            # decision instead of asking again, so a second read cannot undo it or raise again.
            return review(n, _moved(n, ctx), *args, **kwargs)

    return run


def legacy_config(store: StateStore, config: Config, implementer: str | None, now: datetime) -> Config:
    """The pre-#186 derivation for a record with only ``implementer``: only a Claude pause moves
    its review, and availability reads executor pauses alone, as those runs were started."""
    claude_paused = store.executor_paused("claude", now)

    def available(name: str) -> bool:
        entry = scheduler_quota.entry_for(config, name)
        return scheduler_quota.blocked(entry, store, now, {}, "keep", keep=False)[0] is None

    return switched_config(config, implementer, lambda name: claude_paused and name == "claude", available)


from foragent import quota_view, scheduler_quota  # noqa: E402  # Call-time lookups; scheduler_quota imports this module.

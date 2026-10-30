"""Persist GitHub availability waits independently of issue failures."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from foragent.state import StateStore
from foragent.tracker import Tracker, TrackerError


class WaitContext(Protocol):
    tracker: Tracker
    store: StateStore
    now: Callable[[], datetime]


def pause(ctx: WaitContext, error: TrackerError) -> datetime:
    """Prefer response headers; probe once through the injected tracker otherwise."""
    until = error.reset_at
    if until is None:
        try:
            until = ctx.tracker.rate_limit_reset()
            remaining = getattr(ctx.tracker, "rate_limit_remaining", None)
        except (TrackerError, ValueError, TypeError, AttributeError, OverflowError, OSError):
            # A failed reset lookup must not turn throttling into an issue failure.
            until = remaining = None
        if type(remaining) is int:
            # The same response's count, timed by the local clock when it arrived (D93 facts).
            ctx.store.record_github_remaining(remaining, ctx.now())
    now = ctx.now()
    if until is None or until <= now:
        until = now + timedelta(minutes=1)
    return ctx.store.pause_github(until)


def wait_run(ctx: WaitContext, number: int, until: datetime) -> str:
    ctx.store.update_run(number, waiting_until=until.isoformat(), outcome="limit-wait")
    log = ctx.store.log_path(number)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"GitHub rate limit: waiting until {until.isoformat()}\n")
    return "limit-wait"

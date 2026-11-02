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
    """Prefer response headers; probe once otherwise. An outage waits a minute, unprobed, with its cause."""
    until = error.reset_at
    if until is None and error.is_rate_limit:
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
    return ctx.store.pause_github(until, None if error.is_rate_limit else cause(error))


def cause(error: TrackerError) -> str:
    """One line of the actual failure for a wait's log and run detail."""
    return error.diagnostic[:500]


def _line(until: datetime, outage: str | None) -> str:
    return f"GitHub unavailable ({outage}) until {until.isoformat()}" if outage else (
        f"GitHub rate limit until {until.isoformat()}")


def wait_text(store: StateStore, now: datetime, error: TrackerError | None = None) -> str | None:
    """What a wait is for. A caller's own failure names it; another writer may replace the shared
    pause's cause meanwhile. Only a run or tick that merely meets the pause reads that record."""
    if (paused := store.github_wait_until(now, cause=True)) is None:
        return None
    until, outage = paused
    return _line(until, outage if error is None else None if error.is_rate_limit else cause(error))


def wait_run(ctx: WaitContext, number: int, until: datetime, error: TrackerError | None = None) -> str:
    """Record a resumable wait, for ``error`` or for the pause it met, in the log and the run detail."""
    line = wait_text(ctx.store, ctx.now(), error) or _line(until, None if error is None or error.is_rate_limit
                                                           else cause(error))
    ctx.store.update_run(number, waiting_until=until.isoformat(), outcome="limit-wait", detail=line)
    log = ctx.store.log_path(number)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"Waiting: {line}\n")
    return "limit-wait"

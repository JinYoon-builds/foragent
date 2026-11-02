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
    """Prefer response headers; probe once through the injected tracker otherwise.

    An outage (no connection, HTTP 5xx) is not a rate limit: the probe would need the same
    unreachable GitHub, so it waits the one-minute retry on the same shared pause.
    """
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
    return ctx.store.pause_github(until)


def cause(error: TrackerError) -> str:
    """One line of the actual failure for a wait's log and run detail."""
    return " ".join(error.diagnostic.split())[:500]


def wait_run(ctx: WaitContext, number: int, until: datetime, error: TrackerError | None = None) -> str:
    """Record a resumable wait; an outage names its actual diagnostic, not a rate limit."""
    ctx.store.update_run(number, waiting_until=until.isoformat(), outcome="limit-wait")
    log = ctx.store.log_path(number)
    log.parent.mkdir(parents=True, exist_ok=True)
    if error is not None and not error.is_rate_limit:
        line = f"GitHub unavailable ({cause(error)}): waiting until {until.isoformat()}"
    else:
        line = f"GitHub rate limit: waiting until {until.isoformat()}"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    return "limit-wait"

"""The shared boundary for gh subprocess calls and their local per-hour count.

A tick or run context installs a recorder (its state store and clock) when it
wraps its tracker; every gh attempt made afterwards in this process adds one to
the store's counter for the current UTC hour. Without a recorder nothing counts.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import subprocess
import time
from typing import Any

_recorder: tuple[Any, Callable[[], datetime]] | None = None
# (monotonic end, longest single subprocess) while a bounded block runs; None means unbounded.
_deadline: ContextVar[tuple[float, float] | None] = ContextVar("gh_deadline", default=None)


class DeadlineExhausted(Exception):
    """The bounded block has no time left for another gh subprocess."""


@contextmanager
def bounded(total: float, per_call: float) -> Iterator[None]:
    """Give every gh subprocess started inside the block at most ``per_call`` seconds.

    All of them together, retries and ``--paginate`` pages included, end
    ``total`` seconds after the block began on the monotonic clock. A nested
    block can only shorten the time an outer block left.
    """
    end = time.monotonic() + total
    outer = _deadline.get()
    if outer is not None:
        end, per_call = min(end, outer[0]), min(per_call, outer[1])
    token = _deadline.set((end, per_call))
    try:
        yield
    finally:
        _deadline.reset(token)


class Budget:
    """Seconds that several separate bounded blocks share, such as a tick's optional steps."""

    def __init__(self, total: float, per_call: float):
        self.remaining, self.per_call = total, per_call

    @contextmanager
    def spend(self) -> Iterator[None]:
        started = time.monotonic()
        try:
            with bounded(self.remaining, self.per_call):
                yield
        finally:
            self.remaining -= time.monotonic() - started


def call_timeout(default: float) -> float:
    """The timeout for the next gh subprocess; raises DeadlineExhausted when none is left."""
    bound = _deadline.get()
    if bound is None:
        return default
    remaining = bound[0] - time.monotonic()
    if remaining <= 0:
        raise DeadlineExhausted("gh time budget exhausted")
    return min(default, bound[1], remaining)


def set_recorder(store: Any, now: Callable[[], datetime]) -> None:
    global _recorder
    _recorder = (store, now)


def clear_recorder() -> None:
    global _recorder
    _recorder = None


def count_call() -> None:
    """Count one gh attempt, whether it later succeeds, fails or times out."""
    recorder = _recorder
    if recorder is not None:
        store, now = recorder
        store.count_github_call(now())


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run ``gh <args>`` with subprocess.run keyword arguments, counting the attempt.

    Inside ``bounded`` the timeout is cut to the time the block has left.
    """
    if _deadline.get() is not None:
        kwargs["timeout"] = call_timeout(kwargs.get("timeout") or float("inf"))
    count_call()
    return subprocess.run(["gh", *args], **kwargs)

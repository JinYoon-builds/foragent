"""Run budgets, progress and waits credited outside execution time."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from datetime import UTC, datetime, timedelta
import threading
from typing import TYPE_CHECKING

from foragent import capacity, orch_github
from foragent.actions import flag
from foragent.reasons import Reason
from foragent.tracker import TrackerError

if TYPE_CHECKING:
    from foragent.runner import RunContext


def poll_executor[T](execute: Callable[[], T], poll: Callable[[], None]) -> T:
    """Keep execution on the main thread so subprocess termination still works."""
    stopped = threading.Event()

    def watch():
        while not stopped.wait(30):
            poll()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return execute()
    finally:
        stopped.set()
        watcher.join()


def _remaining(n: int, ctx: RunContext) -> int:
    run = ctx.store.get_run(n)
    if run is None:
        raise KeyError(n)
    started = datetime.fromisoformat(run.started_at)
    # Machine sleep is not run time. Waits are timed without sleep, so it is credited once.
    slept, now = capacity.slept_until(started, ctx.now)
    # A run from before #415 may hold sleep in waited_seconds already; it keeps today's budget.
    slept = slept if run.sleep_free_waits else 0.0
    elapsed = (now - started).total_seconds() - run.waited_seconds - slept
    return max(0, int(ctx.config.max_runtime_minutes * 60 - elapsed))


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


@contextmanager
def _test_slot(n: int, ctx: RunContext):
    """Hold a machine-wide test slot; credit the queue time as waited, not run time."""
    run = ctx.store.get_run(n)
    # The wait's sleep window starts at a sensor sample, never at one taken earlier.
    started = capacity.sleep_mark(ctx.now)
    waited = False

    def credit() -> datetime:
        # A wall-clock wait would include sleep, which the runtime limit credits separately.
        slept, now = capacity.slept_until(started, ctx.now)
        queued = (now - started).total_seconds() - slept
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0.0, queued),
            last_progress_at=now.astimezone(UTC).isoformat(),
        )
        return now

    def waiting() -> None:
        nonlocal waited
        waited = True
        now = credit()
        # Tick reads a future waiting_until as alive; refresh it while queued.
        ctx.store.update_run(n, waiting_until=(now + timedelta(seconds=60)).astimezone(UTC).isoformat())

    with ExitStack() as stack:
        try:
            stack.enter_context(capacity.hold_test_slot(ctx.config.test_slots, waiting=waiting))
        finally:
            if waited:
                credit()
                ctx.store.update_run(n, waiting_until=None)
        yield


def _progress(n: int, ctx: RunContext) -> None:
    ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())


def _phase(n: int, ctx: RunContext, phase: str) -> None:
    if ctx.store.get_run(n).phase != phase:
        ctx.store.update_run(n, phase=phase, phase_started_at=ctx.now().astimezone(UTC).isoformat())


def wait_for_rate_limit(n: int, ctx: RunContext, error: TrackerError) -> bool:
    """Persist a resumable wait through the shared GitHub availability mechanism."""
    if not error.is_rate_limit:
        return False
    orch_github.wait_run(ctx, n, orch_github.pause(ctx, error))
    return True


def _executor_wait(n: int, ctx: RunContext, seconds: float) -> None:
    """Persist the wait before sleeping so tick cannot mistake it for a stall."""
    run = ctx.store.get_run(n)
    started = ctx.now()
    started_mono = ctx.monotonic()
    ctx.store.update_run(
        n, waited_seconds=run.waited_seconds + seconds,
        executor_waited_seconds=run.executor_waited_seconds + seconds,
        waiting_until=(started + timedelta(seconds=seconds)).astimezone(UTC).isoformat(),
        last_progress_at=started.astimezone(UTC).isoformat(),
    )
    try:
        ctx.sleep(seconds)
    finally:
        elapsed = max(0, ctx.monotonic() - started_mono)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + elapsed,
            executor_waited_seconds=run.executor_waited_seconds + elapsed,
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )


def _credited_wait(n: int, ctx: RunContext, acquire: Callable[[], object]) -> None:
    """Block in acquire; credit the time as waited and keep tick informed meanwhile."""
    from foragent import runner

    run = ctx.store.get_run(n)
    started = ctx.monotonic()

    def progress():
        now = ctx.now().astimezone(UTC)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=(now + timedelta(seconds=60)).isoformat(), last_progress_at=now.isoformat(),
        )

    progress()
    try:
        runner.poll_executor(acquire, progress)
    finally:
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )

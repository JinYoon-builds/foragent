"""Run budgets, progress and waits credited outside execution time."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

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
    run = _with_open_talk(ctx.store.get_run(n), ctx.now())  # An open talk pauses as it happens (D55).
    if run is None:
        raise KeyError(n)
    elapsed = (ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds() - run.waited_seconds
    return max(0, int(ctx.config.max_runtime_minutes * 60 - elapsed))


def _with_open_talk(run, now: datetime):
    """The run as if its open talk had already been credited to ``waited_seconds``."""
    if run is None:
        return None
    return replace(run, waited_seconds=run.waited_seconds + run.talk_pause_seconds(now))


def paused_seconds(n: int, ctx: RunContext) -> float:
    """All paused time of the run so far, including an open talk (for ``processes.extend_deadline``)."""
    run = ctx.store.get_run(n)
    now = ctx.now()
    return run.waited_seconds + run.talk_pause_seconds(now)


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


@contextmanager
def _test_slot(n: int, ctx: RunContext):
    """Hold a machine-wide test slot; credit the queue time as waited, not run time."""
    started = ctx.now()
    waited = False
    credited = 0.0
    key = f"test-slot-{uuid4().hex}"
    wait = WaitPhase(n, ctx, "test-slot-wait", started)

    def credit(**span) -> datetime:
        nonlocal credited
        now = ctx.now()
        total = max(0.0, (now - started).total_seconds())
        # Increments through the union helper, so a talk or another wait open meanwhile counts once.
        ctx.store.add_waited(n, total - credited, last_progress_at=now.astimezone(UTC).isoformat(), **span)
        credited = total
        return now

    def waiting() -> None:
        nonlocal waited
        # Queued time is its own phase, so facts can see the slot queue and its wait.
        wait.open()
        waited = True
        now = credit(wait="open", key=key, now=ctx.now())
        # Tick reads a future waiting_until as alive; refresh it while queued.
        ctx.store.update_run(n, waiting_until=(now + timedelta(seconds=60)).astimezone(UTC).isoformat())

    with ExitStack() as stack:
        try:
            stack.enter_context(capacity.hold_test_slot(ctx.config.test_slots, waiting=waiting))
        finally:
            if waited:
                now = credit(wait="close", key=key, now=ctx.now())
                ctx.store.update_run(n, waiting_until=None)
                wait.close(now)
        yield


def _progress(n: int, ctx: RunContext) -> None:
    ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())


def phase_identity(run) -> dict:
    """What a nested wait must restore: the phase and the work it was doing."""
    return {"phase": run.phase, "phase_work_id": run.phase_work_id,
            "phase_work_started_at": run.phase_work_started_at}


def restore_phase(n: int, ctx: RunContext, identity: dict) -> None:
    run = ctx.store.get_run(n)
    if phase_identity(run) != identity:
        ctx.store.update_run(n, **identity, phase_started_at=ctx.now().astimezone(UTC).isoformat())


class WaitPhase:
    """One credited wait as its own phase: every place that credits waited time uses this.

    The wait phase starts at ``started`` (so the interrupted phase closes there) and the
    interrupted phase and work identity come back when it ends. ``open()`` may be called
    lazily; a wait that ended without opening but took time is still journaled.
    """

    def __init__(self, n: int, ctx: RunContext, phase: str, started: datetime | None = None):
        self.n, self.ctx, self.phase = n, ctx, phase
        self.started = started or ctx.now()
        self.previous = phase_identity(ctx.store.get_run(n))
        self.opened = False

    def open(self) -> None:
        if not self.opened and self.previous["phase"] != self.phase:
            self.ctx.store.update_run(self.n, phase=self.phase,
                                      phase_started_at=self.started.astimezone(UTC).isoformat())
            self.opened = True

    def close(self, now: datetime | None = None) -> None:
        now = now or self.ctx.now()
        if not self.opened and now > self.started:
            self.open()
        if self.opened:
            self.ctx.store.update_run(self.n, **self.previous, phase_started_at=now.astimezone(UTC).isoformat())
            self.opened = False


@contextmanager
def in_phase(n: int, ctx: RunContext, phase: str):
    """Run a block as ``phase`` (e.g. checking), then give back the interrupted phase and work."""
    previous = phase_identity(ctx.store.get_run(n))
    if previous["phase"] == phase:
        yield
        return
    _phase(n, ctx, phase)
    try:
        yield
    finally:
        restore_phase(n, ctx, previous)


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
    wait = WaitPhase(n, ctx, "executor-wait", started)
    wait.open()
    key = f"executor-wait-{uuid4().hex}"  # Concurrent reviewers back off under their own keys.
    ctx.store.add_waited(
        n, seconds, wait="open", key=key, now=started,
        executor_waited_seconds=run.executor_waited_seconds + seconds,
        waiting_until=(started + timedelta(seconds=seconds)).astimezone(UTC).isoformat(),
        last_progress_at=started.astimezone(UTC).isoformat(),
    )
    try:
        ctx.sleep(seconds)
    finally:
        elapsed = max(0, ctx.monotonic() - started_mono)
        ctx.store.add_waited(
            n, elapsed - seconds, wait="close", key=key, now=ctx.now(),
            executor_waited_seconds=run.executor_waited_seconds + elapsed,
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )
        wait.close()


def _credited_wait(
    n: int, ctx: RunContext, acquire: Callable[[], object], phase: str, *, eager: bool = True,
) -> None:
    """Block in acquire as the ``phase`` wait; credit the time as waited and keep tick informed.

    ``eager=False`` opens the wait phase only once the wait lasts (a lock that is usually
    free adds no phase events when it is acquired at once).
    """
    from foragent import runner

    started = ctx.monotonic()
    wait = WaitPhase(n, ctx, phase)
    if eager:
        wait.open()
    credited = 0.0
    key = f"lock-wait-{uuid4().hex}"

    def credit(**fields):
        nonlocal credited
        total = max(0, ctx.monotonic() - started)
        ctx.store.add_waited(n, total - credited, **fields)
        credited = total

    def progress(polled: bool = True, **span):
        if polled:
            wait.open()
        now = ctx.now().astimezone(UTC)
        credit(waiting_until=(now + timedelta(seconds=60)).isoformat(), last_progress_at=now.isoformat(), **span)

    progress(polled=False, wait="open", key=key, now=ctx.now())
    try:
        runner.poll_executor(acquire, progress)
    finally:
        credit(waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
               wait="close", key=key, now=ctx.now())
        wait.close()
"""Run budgets, progress and waits credited outside execution time."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from datetime import UTC, datetime, timedelta
import threading
from typing import TYPE_CHECKING

from foragent import capacity, orch_github
from foragent.actions import flag
from foragent.config import safety_net_seconds
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


def _elapsed(n: int, ctx: RunContext) -> float:
    run = ctx.store.get_run(n)
    if run is None:
        raise KeyError(n)
    # The gate tick credits machine sleep to the run record; the runner never reads the sensor.
    return ((ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds()
            - run.waited_seconds - run.slept_seconds)


def _remaining(n: int, ctx: RunContext) -> int:
    """What is left of ``max_runtime_minutes`` (credited waits excluded).

    Waiting loops (CI polling, merge turns) keep it as their bound, as before #435. An
    executor session gets it as ``timeout_s``, which run_logged's ``quiet_timeout`` applies
    only once the session is also quiet; a check gets it only as its logged limit.
    """
    deadline = getattr(ctx, "deadline_seconds", None)  # Optional: only a probe (fa doctor) sets one.
    limit = ctx.config.max_runtime_minutes * 60 if deadline is None else deadline
    return max(0, int(limit - _elapsed(n, ctx)))


def _budget_left(n: int, ctx: RunContext) -> int:
    """The run's own bound at step boundaries (#435): the safety net plus a margin.

    The gate's tick reaches the safety net first and ends the run resumably, keeping its
    WIP; this bound only stops a runner whose gate stopped ticking. A probe's deadline wins.
    """
    from foragent import runner

    deadline = getattr(ctx, "deadline_seconds", None)
    if deadline is not None:
        return runner._remaining(n, ctx)
    return max(0, int(safety_net_seconds(ctx.config) + RUNNER_MARGIN_SECONDS - _elapsed(n, ctx)))


RUNNER_MARGIN_SECONDS = 10 * 60


def past_runtime_limit(n: int, ctx: RunContext) -> bool:
    """Past ``max_runtime_minutes``: a limit only on work that is also silent (#435).

    Callers stop at a step boundary only when the step just finished was itself
    stopped by silence; progressing work runs on to the safety net.
    """
    limit = ctx.config.max_runtime_minutes
    run = ctx.store.get_run(n)
    elapsed = ((ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds()
               - run.waited_seconds - run.slept_seconds)
    return elapsed >= limit * 60  # Like the old budget: reaching the limit is past it.


def watchdog_restart(n: int, ctx: RunContext, detail: str):
    """A check the watchdog stopped ends the run as stalled-restart, back to ready (#435).

    The same rule as the gate's silence stop: the kept WIP and gathered failures reach
    the next run (with an open PR, the issue is flagged stalled instead). Raises.
    """
    from types import SimpleNamespace

    from foragent import scheduler
    from foragent.run_attempt import ExecutorStopped

    _phase_log(ctx, n, detail)
    ctx.store.update_run(n, outcome="stalled-restart")
    scheduler._restore_stalled(ctx, n, 1, SimpleNamespace(actions=[]))
    raise ExecutorStopped("stalled-restart", detail)


def after_check(n: int, ctx: RunContext, checks: list) -> None:
    """Progress after a check; one the watchdog stopped at the safety net restarts the run (#435).

    A silent check stays a failing result for the implementer; the safety net always means
    the run ends resumably, keeping its WIP and every failure gathered so far.
    """
    _progress(n, ctx)
    if checks and getattr(checks[-1], "stop", None) == "safety-net":
        from foragent.run_checks import save_failures

        save_failures(ctx.store.root, n, checks, complete=False)
        watchdog_restart(n, ctx, f"A check reached the safety net: {checks[-1].command}")


def _phase_log(ctx: RunContext, n: int, detail: str) -> None:
    from foragent.run_checks import _append

    _append(ctx.store.log_path(n), detail)


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


@contextmanager
def _test_slot(n: int, ctx: RunContext):
    """Hold a machine-wide test slot; credit the queue time as waited, not run time."""
    run = ctx.store.get_run(n)
    started = ctx.now()
    waited = False
    wait = WaitPhase(n, ctx, "test-slot-wait", started)

    def credit() -> datetime:
        now = ctx.now()
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0.0, (now - started).total_seconds()),
            last_progress_at=now.astimezone(UTC).isoformat(),
        )
        return now

    def waiting() -> None:
        nonlocal waited
        # Queued time is its own phase, so facts can see the slot queue and its wait.
        wait.open()
        waited = True
        now = credit()
        # Tick reads a future waiting_until as alive; refresh it while queued.
        ctx.store.update_run(n, waiting_until=(now + timedelta(seconds=60)).astimezone(UTC).isoformat())

    with ExitStack() as stack:
        try:
            stack.enter_context(capacity.hold_test_slot(ctx.config.test_slots, waiting=waiting))
        finally:
            if waited:
                now = credit()
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
        wait.close()


def _credited_wait(
    n: int, ctx: RunContext, acquire: Callable[[], object], phase: str, *, eager: bool = True,
) -> None:
    """Block in acquire as the ``phase`` wait; credit the time as waited and keep tick informed.

    ``eager=False`` opens the wait phase only once the wait lasts (a lock that is usually
    free adds no phase events when it is acquired at once).
    """
    from foragent import runner

    run = ctx.store.get_run(n)
    started = ctx.monotonic()
    wait = WaitPhase(n, ctx, phase)
    if eager:
        wait.open()

    def progress(polled: bool = True):
        if polled:
            wait.open()
        now = ctx.now().astimezone(UTC)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=(now + timedelta(seconds=60)).isoformat(), last_progress_at=now.isoformat(),
        )

    progress(polled=False)
    try:
        runner.poll_executor(acquire, progress)
    finally:
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )
        wait.close()

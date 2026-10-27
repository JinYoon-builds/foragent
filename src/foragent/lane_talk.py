"""The gate tick's side of a talk (D55 addendum, D95): the only writer of a run's talk state.

A run's current talk is derived from the orch events journal (``state_talk``). Each
tick probes it with the shared liveness function (``lane_talk_probe``). Only closed
writes: one ``talk-closed`` event and the paused time credited once through the
union helper. Open or unknown keep the talk, and stall and runtime checks stay
paused. A newer ``talk-opened`` event supersedes an older one, which closes at the
newer event's time. Opening terminals and sending messages live in ``lane_talk_open``.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from foragent import lane_talk_probe as probe
from foragent.i18n import text
from foragent.state import StateStore
from foragent.state_locks import LockBusy
from foragent.state_records import Run


def check_talking(store: StateStore, run: Run, root: Path, now: datetime) -> tuple[Run, bool]:
    """Tick step for a talking run: ``(run, still_talking)``; only a confirmed closure ends it."""
    current, superseded = store.talk_state(run)
    for event, newer_at in superseded:
        store.close_talk(run.number, event, datetime.fromisoformat(newer_at))
    if current is None:
        return store.get_run(run.number) or run, False
    cwd = Path(run.worktree) if Path(run.worktree).is_dir() else root
    target = probe.resolved(current, cwd)  # A pending title found now serves this probe only.
    if target is None or probe.liveness(target, cwd) != probe.CLOSED:
        return store.get_run(run.number) or run, True
    store.close_talk(run.number, current, now)
    return store.get_run(run.number) or run, False


def talking_now(store: StateStore, number: int) -> bool:
    """Read again right before a stop for time: an open (or just opened) talk pauses it."""
    run = store.get_run(number)
    return run is not None and run.talking


def guarded_stop(ctx, number: int, report, stop: Callable[[object], str],
                 mark: Callable[[], object] | None = None) -> str:
    """A stall or runtime stop under ``worktree_lock(n)``, taken without waiting.

    ``fa lanes talk`` holds the same lock from its decision to its talk-opened event, so a
    talk cannot open between this re-read and the signal. A busy lock skips the stop for
    this tick (one report line). The lock is released right after ``ctx.kill_runner``,
    before the executor stop, the worktree cleanup (which takes the lock itself) and the
    outcome updates; ``stop`` receives the context whose ``kill_runner`` does that. ``mark``
    (the stall restart and outcome, which the runner reads when the signal arrives) is written
    only here: after the talk was read again and the run is still running, right before the
    signal. A skipped stop ("skipped") has written nothing.
    """
    held = ExitStack()
    try:
        held.enter_context(ctx.store.worktree_lock(number, timeout=0))
    except LockBusy:
        report.actions.append(text("talk_stop_busy", ctx.config.language, n=number))
        return "skipped"
    try:
        current = ctx.store.get_run(number)
        if current is None or current.status != "running" or current.talking:
            return "skipped"
        if mark is not None:
            mark()

        def kill_runner(pid):
            try:
                return ctx.kill_runner(pid)
            finally:
                held.close()

        guarded = copy.copy(ctx)
        object.__setattr__(guarded, "kill_runner", kill_runner)
        return stop(guarded)
    finally:
        held.close()


def stall_since(store: StateStore, run: Run) -> datetime:
    """The stall clock's start: the later of the last progress and the latest talk closing."""
    times = [value for value in (run.last_progress_at or run.started_at, store.talk_closed_at(run)) if value]
    return max(datetime.fromisoformat(value) for value in times)

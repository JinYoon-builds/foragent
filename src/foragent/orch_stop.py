"""`fa orch stop N --keep-work --why`: stop a running issue through the stall-kill path.

The command takes the tick lock, so it is the only kill writer besides the tick,
checks #261's rules on the recorded PIDs without signaling, and calls
scheduler._kill_run(outcome="stopped"): the existing path terminates the tree and
saves the WIP (named `[stopped: <why>]`). _restore_stopped then returns the issue
to ready with one stop event. Every unfinished record `fa gate status` lists is
stoppable; when its workers are already gone the same path saves the work (#1207).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from foragent import processes
from foragent.i18n import text
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.runner import RunContext

TICK_LOCK_WAIT_S = 60


@contextmanager
def _tick_lock(ctx: RunContext, n: int) -> Iterator[None]:
    """Hold the gate's tick lock, polling for up to TICK_LOCK_WAIT_S while a tick holds it."""
    from foragent.orch import OrchError

    deadline = ctx.monotonic() + TICK_LOCK_WAIT_S
    while True:
        with ctx.store.hold_tick_lock() as held:
            if held:
                yield
                return
        if ctx.monotonic() >= deadline:
            raise OrchError(text("orch_stop_busy", ctx.config.language, n=n))
        ctx.sleep(1)


def unsafe_target(run: Run) -> tuple[str, int, str] | None:
    """The first recorded PID #261 forbids signaling, as (role, pid, rule); read without any signal.

    The rules are processes._protected()'s lineage, group and session, PID 1 or
    lower, and a start time that differs from the recorded identity. A PID with
    no recorded identity, or an unreadable process list, is left to _kill_run,
    which applies #261's own checks.
    """
    try:
        rows = {row.pid: row for row in processes.list_processes()}
    except (OSError, PermissionError):
        return None
    lineage, group, session = processes._protected(rows)
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    for role, pid in (("runner", run.pid), ("executor", run.executor_pgid)):
        if pid is None:
            continue
        row = rows.get(pid)
        if pid <= 1 or pid in lineage:
            return role, pid, "this command's own lineage" if pid > 1 else "PID 1 or lower"
        if row is None:
            continue
        if row.pgid in lineage or row.sid in lineage or row.pgid == group or row.sid == session:
            return role, pid, "this command's process group or session"
        if pid in identities and identities[pid] != row.start_time:
            return role, pid, "a different process now has this PID"
    return None


def stop(ctx: RunContext, n: int, why: str) -> str:
    """Stop issue N's run through scheduler._kill_run and return it to ready, keeping its work."""
    from foragent import scheduler
    from foragent.orch import OrchError
    from foragent.scheduler_stop import _restore_stopped, unfinished_runs

    language = ctx.config.language
    why = " ".join(why.split())
    if not why:
        raise OrchError(text("orch_stop_why_required", language))
    with _tick_lock(ctx, n):
        # The fact `fa gate status` lists as active_runs: every unfinished issue record,
        # with or without live workers (#1207). Process liveness only picks the kill path.
        run = next((run for run in unfinished_runs(ctx.store) if run.number == n), None)
        if run is None:
            raise OrchError(text("orch_stop_not_running", language, n=n))
        if (unsafe := unsafe_target(run)) is not None:
            role, pid, rule = unsafe
            raise OrchError(text("orch_stop_unsafe", language, n=n, role=role, pid=pid, rule=rule))
        tick_ctx = scheduler.TickContext(
            repo=ctx.repo, repo_root=ctx.repo_root, tracker=ctx.tracker, store=ctx.store,
            config=ctx.config, git=ctx.git, now=ctx.now, launcher=lambda number: None,
        )
        report = scheduler.TickReport()
        if not scheduler._kill_run(tick_ctx, n, report, outcome="stopped", detail=why):
            # The kill path's own changes (a refused stop, a dead run's recovery) stand.
            if not report.actions:
                raise OrchError(text("orch_stop_not_running", language, n=n))
            raise OrchError(text("orch_stop_failed", language, n=n, actions="; ".join(report.actions)))
        _restore_stopped(tick_ctx, n, why, report)
    return text("orch_stop_done", language, n=n, why=why)  # _restore_stopped posted it as the audit comment.

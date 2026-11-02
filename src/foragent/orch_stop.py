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
            _note(ctx, n, None, "skipped", "busy")
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


def _note(ctx: RunContext, n: int, run: Run | None, decision: str, reason: str, detail: str | None = None) -> None:
    """An issue stop's decision; repeating the latest one is no new record, across executions too."""
    ctx.store.record_flow_change("stop", issue=n, run_started_at=run.started_at if run is not None else None,
                                 across_runs=True, decision=decision, reason=reason, detail=detail)


def _note_not_stopped(ctx: RunContext, tick_ctx, n: int, report) -> None:
    """Why the kill path returned without stopping; a refusal it already recorded is not said again."""
    from foragent.scheduler_stop import _stop_already_refused

    try:
        run = ctx.store.get_run(n)
        running = run is not None and run.status == "running"
        if running and _stop_already_refused(tick_ctx, run):
            return
        if report.actions:
            _note(ctx, n, run, "skipped", "failed", "; ".join(report.actions))
        else:
            _note(ctx, n, run, "skipped", "run-changed" if running else "not-running")
    except Exception:  # noqa: BLE001 - an observation never replaces the stop's own error
        return


def stop(ctx: RunContext, n: int, why: str) -> str:
    """Stop issue N's run through scheduler._kill_run and return it to ready, keeping its work."""
    from foragent import scheduler
    from foragent.orch import OrchError
    from foragent.scheduler_stop import _restore_stopped, unfinished_runs

    language = ctx.config.language
    why = " ".join(why.split())
    if not why:
        _note(ctx, n, None, "skipped", "why-required")
        raise OrchError(text("orch_stop_why_required", language))
    from foragent import orch_scope

    if (refusal := orch_scope.check(ctx, n)) is not None:
        raise OrchError(refusal)
    with _tick_lock(ctx, n):
        # The fact `fa gate status` lists as active_runs: every unfinished issue record,
        # with or without live workers (#1207). Process liveness only picks the kill path.
        run = next((run for run in unfinished_runs(ctx.store) if run.number == n), None)
        if run is None:
            _note(ctx, n, None, "skipped", "not-running")
            raise OrchError(text("orch_stop_not_running", language, n=n))
        if (unsafe := unsafe_target(run)) is not None:
            role, pid, rule = unsafe
            _note(ctx, n, run, "skipped", "unsafe", f"{role} {pid}: {rule}")
            raise OrchError(text("orch_stop_unsafe", language, n=n, role=role, pid=pid, rule=rule))
        tick_ctx = scheduler.TickContext(
            repo=ctx.repo, repo_root=ctx.repo_root, tracker=ctx.tracker, store=ctx.store,
            config=ctx.config, git=ctx.git, now=ctx.now, launcher=lambda number: None,
        )
        tick_ctx._scope = ctx._scope  # Inner paths judge from this command's one snapshot (#1004).
        report = scheduler.TickReport()
        try:
            killed = scheduler._kill_run(tick_ctx, n, report, outcome="stopped", detail=why)
        except Exception as exc:
            _note(ctx, n, run, "skipped", "failed", f"{type(exc).__name__}: {exc}")
            raise
        if not killed:
            # The kill path's own changes (a refused stop, a dead run's recovery) stand.
            _note_not_stopped(ctx, tick_ctx, n, report)
            if not report.actions:
                raise OrchError(text("orch_stop_not_running", language, n=n))
            raise OrchError(text("orch_stop_failed", language, n=n, actions="; ".join(report.actions)))
        _note(ctx, n, run, "stopped", "requested", why)  # The kill stands even if a later restore step fails.
        _restore_stopped(tick_ctx, n, why, report)
    return text("orch_stop_done", language, n=n, why=why)  # _restore_stopped posted it as the audit comment.

"""Handle stop refusals without aborting scheduler ticks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent import processes, scheduler
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _stop_already_refused(ctx: TickContext, run: Run) -> bool:
    previous = ctx.store.load_flag(run.number)
    return (previous is not None and previous.reason == scheduler.Reason.STOP_REFUSED
            and scheduler.datetime.fromisoformat(previous.flagged_at) >= scheduler.datetime.fromisoformat(run.started_at))


def _flag_stop_refused(ctx: TickContext, run: Run, report: TickReport, error: Exception) -> None:
    if _stop_already_refused(ctx, run):
        return
    summary = f"Stop refused: {error}"
    scheduler.flag(ctx, run.number, scheduler.Reason.STOP_REFUSED, summary)
    scheduler.append_event(ctx.store, ctx.now(), run.number, "stop-refused", summary)
    report.actions.append(f"#{run.number} {summary}")


def _pid_exists(pid: int) -> bool:
    # Keep the scheduler's established liveness seam; signal 0 delivers nothing.
    try:
        scheduler.os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_processes_exist(run: Run) -> bool:
    rows = processes.list_processes()
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    return (any(_pid_exists(pid) for pid in (run.pid, run.executor_pgid) if pid is not None)
            or any((run.executor_pgid is not None and row.pgid == run.executor_pgid)
                   or identities.get(row.pid) == row.start_time for row in rows))


def _stop_run_processes(
    ctx: TickContext, run: Run, report: TickReport, *, stop_runner: bool = True,
) -> str:
    """A refused stop belongs to this run, never to the whole gate tick."""
    try:
        if _stop_already_refused(ctx, run):
            return "refused" if _run_processes_exist(run) else "gone"
        if stop_runner and run.pid is not None:
            with processes.recorded_tree(run.process_identities):
                ctx.kill_runner(run.pid)
        scheduler._kill_executor(ctx, run.number)
    except (ValueError, PermissionError, TimeoutError) as exc:
        try:
            if not _run_processes_exist(ctx.store.get_run(run.number) or run):
                return "gone"
        except PermissionError:
            pass  # An unreadable table cannot prove death.
        _flag_stop_refused(ctx, run, report, exc)
        return "refused"
    return "stopped"

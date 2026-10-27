"""Handle stop refusals without aborting scheduler ticks."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from foragent import processes, scheduler
from foragent.orch_tick_outcome import step
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def runner_process_exists(number: int) -> bool:
    snapshot = scheduler.current_snapshot()
    if snapshot is not None and snapshot.available and snapshot.runner_exists(number):
        return True
    try:
        output = scheduler.subprocess.run(
            ["ps", "-axo", "pid,command"], check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, scheduler.subprocess.CalledProcessError):
        return True  # An unavailable process list cannot establish runner death.
    return scheduler.runner_commands_exist(number, (fields[1] for line in output.splitlines()
                                                    if len(fields := line.split(maxsplit=1)) == 2 and fields[0].isdigit()))


@contextmanager
def tick_processes(store):
    """Unavailable routine evidence defers pruning without failing the tick."""
    with ExitStack() as stack:
        with step("process_snapshot"):
            snapshot = stack.enter_context(processes.process_snapshot())
            if snapshot.available:
                store.prune_process_identities(inactive_only=True)
        try:
            yield snapshot
        finally:
            if snapshot.available:
                with step("process_prune"):
                    store.prune_process_identities()


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


def _restore_stopped(ctx: TickContext, number: int, why: str, report: TickReport) -> None:
    """After `fa orch stop`'s kill: ready again, as a stall restart, with one stop event naming the why.

    The run's WIP was already saved by the kill path. An open PR flags the issue as
    _restore_stalled does; a closed, blocked or needs-human issue only loses running.
    A stop counts toward neither the crash streak nor the stall restart budget.
    """
    issue = ctx.tracker.get_issue(number)
    if issue.state != "open" or issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
    elif ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, scheduler.Reason.STALLED,
                       "Runner stopped with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged stalled with open PR")
    else:
        ctx.tracker.add_labels(number, [scheduler.READY])
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
        report.actions.append(f"#{number} returned to ready after stop")
        scheduler.board.sync_status(ctx, number, "Ready")
    scheduler.append_event(ctx.store, ctx.now(), number, "stop",
                           f"Stopped by fa orch stop; unfinished work kept on foragent/issue-{number}. Why: {why}")

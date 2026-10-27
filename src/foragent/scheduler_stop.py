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
    if run.kind != "issue":
        return False  # A dream has no issue flag; its refusal is reported on every tick instead.
    previous = ctx.store.load_flag(run.number)
    return (previous is not None and previous.reason == scheduler.Reason.STOP_REFUSED
            and scheduler.datetime.fromisoformat(previous.flagged_at) >= scheduler.datetime.fromisoformat(run.started_at))


def _flag_stop_refused(ctx: TickContext, run: Run, report: TickReport, error: Exception) -> None:
    if _stop_already_refused(ctx, run):
        return
    summary = f"Stop refused: {error}"
    if run.kind != "issue":  # Never flag issue #0: a dream's refusal touches no issue labels.
        report.actions.append(f"lane {run.lane} dream {summary}")
        return
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


# The fields a runtime or stall decision reads; a change after the check voids it (_kill_run).
DECISION_FIELDS = ("last_progress_at", "waiting_until", "waited_seconds", "slept_seconds", "slept_since_progress")


def runner_alive(run: Run, exists=None) -> bool | None:
    """The one liveness rule for a recorded runner (issue or dream) with a pid.

    It is alive when its pid exists with the recorded start time; a reused pid (another
    start time) is dead. With no recorded start time (no birth identity), it is alive
    while the pid exists. A pid the listing could not read is unknown (None), never dead.
    ``exists`` is the caller's signal-0 probe (recovery passes its own, patchable one).
    """
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    if run.pid in identities:
        table = processes.list_processes()
        if run.pid in getattr(table, "unreadable", frozenset()):
            return None  # Unknown liveness is not confirmed death (D95).
        if not any(row.pid == run.pid and row.start_time == identities[run.pid] for row in table):
            return False  # Never probe a replacement as if it were the recorded runner.
    return (exists or _pid_exists)(run.pid)


def _run_processes_exist(run: Run) -> bool:
    """Whether any recorded process may remain; an unreadable recorded pid counts (unknown is not gone)."""
    rows = processes.list_processes()
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    unreadable = getattr(rows, "unreadable", frozenset())
    return (any(_pid_exists(pid) for pid in (run.pid, run.executor_pgid) if pid is not None)
            or bool(unreadable & {run.pid, run.executor_pgid, *identities})
            or any((run.executor_pgid is not None and row.pgid == run.executor_pgid)
                   or identities.get(row.pid) == row.start_time for row in rows))


def _stop_unconfirmed(run: Run) -> bool:
    """Whether a stop that raised nothing may still have left the run's processes alive.

    kill_process_group waits until every readable recorded identity is gone, so only a
    recorded pid the listing cannot read stays unconfirmed. A runner recorded without a
    birth identity is trusted to the injected killer, as the locked stop tests require.
    """
    identities = {item["pid"] for item in run.process_identities}
    unreadable = getattr(processes.list_processes(), "unreadable", frozenset())
    return bool(unreadable & {run.pid, run.executor_pgid, *identities})


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
        scheduler._kill_executor(ctx, run.key)
        if _stop_unconfirmed(run):
            raise ValueError("the stop cannot be confirmed: a recorded process is unreadable or has no birth identity")
    except (ValueError, PermissionError, TimeoutError) as exc:
        try:
            if not _run_processes_exist(ctx.store.get_run(run.key) or run):
                return "gone"
        except PermissionError:
            pass  # An unreadable table cannot prove death.
        _flag_stop_refused(ctx, run, report, exc)
        return "refused"
    return "stopped"

"""Handle stop refusals without aborting scheduler ticks."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
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
# The fields a dead decision reads: a worker that registered or progressed meanwhile is not dead.
LIVENESS_FIELDS = ("pid", "process_identities", "executor_pgid", "last_progress_at")


def _listing(pids=()):
    """The process table, or None when it cannot be read (no evidence either way).

    Stopping a tree the watchdog proved silent (#435) reads only the run's own ``pids``,
    one targeted identity read each: the tick's single snapshot plus these rechecks.
    """
    if pids and processes.in_quiescent_stop():
        return processes.targeted_table(pids)
    try:
        return processes.list_processes()
    except OSError:  # PermissionError included.
        return None


def runner_alive(run: Run, exists=None) -> bool | None:
    """The one liveness rule for a recorded runner (issue or dream) with a pid.

    It is alive when its pid exists with the recorded start time; a reused pid (another
    start time) is dead. With no recorded start time (no birth identity), it is alive
    while the pid exists. A pid the listing could not read, or a table that cannot be read at
    all, is unknown (None), never dead and never an error.
    ``exists`` is the caller's signal-0 probe (recovery passes its own, patchable one).
    """
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    if run.pid in identities:
        if (table := _listing()) is None:
            return None
        if run.pid in getattr(table, "unreadable", frozenset()):
            return None  # Unknown liveness is not confirmed death (D95).
        if not any(row.pid == run.pid and row.start_time == identities[run.pid] for row in table):
            return False  # Never probe a replacement as if it were the recorded runner.
    return (exists or _pid_exists)(run.pid)


def _run_processes_exist(run: Run) -> bool:
    """Whether any recorded process may remain; an unreadable pid or table counts (unknown is not gone)."""
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    if (rows := _listing({pid for pid in (run.pid, run.executor_pgid, *identities) if pid is not None})) is None:
        return True
    unreadable = getattr(rows, "unreadable", frozenset())
    return (any(_pid_exists(pid) for pid in (run.pid, run.executor_pgid) if pid is not None)
            or bool(unreadable & {run.pid, run.executor_pgid, *identities})
            or any((run.executor_pgid is not None and row.pgid == run.executor_pgid)
                   or identities.get(row.pid) == row.start_time for row in rows))


def recorded_member_alive(item: dict) -> bool:
    """One targeted read of a recorded identity; an unreadable one cannot prove death."""
    try:
        row = processes.read_process_identity(item["pid"])
    except OSError:
        return True
    return row is not None and row.start_time == item["start_time"]


def _registration(run: Run) -> tuple:
    starts = {item["pid"]: item["start_time"] for item in run.process_identities}
    return run.pid, starts.get(run.pid), run.last_progress_at


def registered_since(ctx: TickContext, run: Run) -> bool:
    """Whether a decision taken from ``run`` is stale: the record finished, or its runner
    registered (pid, birth identity) or progressed since. The caller then leaves it for the
    next tick instead of removing the worktree or finishing the record (D95)."""
    fresh = ctx.store.get_run(run.key)
    return fresh is None or fresh.status != "running" or _registration(fresh) != _registration(run)


def _stop_unconfirmed(run: Run) -> bool:
    """Whether a stop that raised nothing may still have left the run's processes alive.

    kill_process_group waits until every readable recorded identity is gone, so a recorded
    pid the listing cannot read stays unconfirmed. A dream's worker or executor recorded
    without a birth identity (a null start time) is alive while its pid exists (D95), and
    the recorded tree does not reach it. An issue runner without one is trusted to the
    injected killer, as the locked stop tests require.
    """
    identities = {item["pid"] for item in run.process_identities}
    roots = {pid for pid in (run.pid, run.executor_pgid) if pid is not None}
    if (table := _listing(roots | identities)) is None:
        return True  # No table: nothing about the stop is confirmed.
    unreadable = getattr(table, "unreadable", frozenset())
    return bool(unreadable & {*roots, *identities}) or (
        run.kind != "issue" and any(pid not in identities and _pid_exists(pid) for pid in roots))


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


def _restore_stopped(ctx: TickContext, number: int, why: str, report: TickReport) -> None:
    """After `fa orch stop`'s kill: ready again, as a stall restart, with one stop event naming the why.

    The run's WIP was already saved by the kill path. The stop event and the orch
    audit comment come first, once per run, while `running` still marks the issue
    for orphan recovery: a later step that fails (a board rate limit) is repeated by
    the next tick and the record is never lost or written twice. `running` goes
    last. An open PR flags the issue as _restore_stalled does; a closed, blocked or
    needs-human issue only loses running. A stop counts toward neither the crash
    streak nor the stall restart budget.
    """
    from foragent.i18n import text

    run = ctx.store.get_run(number)
    journal = ctx.store.root / "orch-events.jsonl"
    started = scheduler.datetime.fromisoformat(run.started_at) if run is not None else None
    if not (journal.exists() and any(
        event.get("issue") == number and event.get("kind") == "stop"
        and (started is None or scheduler.datetime.fromisoformat(event["at"]) >= started)
        for event in map(json.loads, journal.read_text(encoding="utf-8").splitlines())
    )):
        ctx.tracker.comment(number, "<!-- foragent:orch -->\n" + text(
            "orch_stop_done", ctx.config.language, n=number, why=why))
        scheduler.append_event(ctx.store, ctx.now(), number, "stop",
                               f"Stopped by fa orch stop; unfinished work kept on foragent/issue-{number}. Why: {why}")
    issue = ctx.tracker.get_issue(number)
    if issue.state != "open" or issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
    elif ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, scheduler.Reason.STALLED,
                       "Runner stopped with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged stalled with open PR")
    else:
        ctx.tracker.add_labels(number, [scheduler.READY])
        scheduler.board.sync_status(ctx, number, "Ready")
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
        report.actions.append(f"#{number} returned to ready after stop")

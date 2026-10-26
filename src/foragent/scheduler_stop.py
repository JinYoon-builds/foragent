"""Handle stop refusals without aborting scheduler ticks."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import processes, scheduler
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
    with processes.process_snapshot() as snapshot:
        if snapshot.available:
            store.prune_process_identities(inactive_only=True)
        try:
            yield snapshot
        finally:
            if snapshot.available:
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


def stop_target_refusal(run: Run) -> str | None:
    """Why a recorded runner or executor PID fails #261's rules, read without signaling.

    A live recorded PID must be a recorded identity outside PID 1 and this
    process's own lineage, group and session. An unreadable one, or a runner
    PID that is missing or was never recorded as an identity, cannot be
    verified and is refused; a dead recorded one is left to the stop path.
    """
    try:
        table = processes.list_processes()
    except PermissionError as exc:
        return str(exc)
    rows = {row.pid: row for row in table}
    unreadable = getattr(table, "unreadable", frozenset())
    lineage, group, session = processes._protected(rows)
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    for name, pid in (("runner", run.pid), ("executor", run.executor_pgid)):
        if pid is None:
            continue
        if pid in unreadable:
            return f"recorded {name} PID {pid} cannot be read; ownership cannot be verified"
        row = rows.get(pid)
        if pid <= 1 or pid in lineage or (row is not None and (
            identities.get(pid) != row.start_time or row.pgid in lineage or row.sid in lineage
            or row.pgid == group or row.sid == session
        )):
            return f"recorded {name} PID {pid} is not a process Foragent started for this run"
    # Missing evidence is never proof of safety: without a recorded runner
    # identity the stop could neither verify nor signal the process it ends.
    if run.pid is None:
        return "no runner PID is recorded for this run; ownership cannot be verified"
    if run.pid not in identities:
        return f"recorded runner PID {run.pid} has no recorded process identity; ownership cannot be verified"
    return None


def stop_summary(number: int, why: str) -> str:
    return f"#{number}: stopped and kept unfinished work on foragent/issue-{number}; why: {why}"


def _pending(store, number: int) -> Path:
    return store.root / "stops" / f"{number}.json"


def mark_stop_pending(store, run: Run, why: str) -> None:
    """Durably record a stop before its intent, signal or labels; only completion removes it.

    Recovery reads this record, never a label an earlier step (the stop's own, or
    a dying runner's flag) may already have removed.
    """
    path = _pending(store, run.number)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._atomic_write(path, json.dumps({"number": run.number, "started_at": run.started_at, "why": why}))


def drop_stop_pending(store, number: int) -> None:
    _pending(store, number).unlink(missing_ok=True)


def complete_stop(ctx: TickContext, run: Run) -> str:
    """Finish an issue whose run `fa orch stop` ended; returns a summary note.

    The stop event is required, so it is journaled first and once per run. Each
    label step is separate and idempotent, and the pending record is removed only
    after all of them succeed, so any failed step is repeated by the next tick.
    As `fa orch retry`, the issue returns to ready: a hold or this issue's own
    open PR (which a tick would otherwise read as review) is cleared, and the
    next run resumes from the issue branch. A closed issue only loses running.
    """
    number = run.number
    journal = ctx.store.root / "orch-events.jsonl"
    started = scheduler.datetime.fromisoformat(run.started_at)
    recorded = journal.exists() and any(
        event.get("issue") == number and event.get("kind") == "stop"
        and scheduler.datetime.fromisoformat(event["at"]) >= started
        for event in map(json.loads, journal.read_text(encoding="utf-8").splitlines())
    )
    if not recorded:
        scheduler.append_event(ctx.store, ctx.now(), number, "stop", stop_summary(number, run.stop_why))
    issue = ctx.tracker.get_issue(number)
    note, removals = "; the issue is closed, so it was not returned to ready", [scheduler.RUNNING]
    if issue.state == "open":
        note = ""
        if (pr := ctx.tracker.find_pr(f"foragent/issue-{number}")) is not None:
            ctx.tracker.close_pr(pr, f"Closed by Foragent orchestrator stop; the next run resumes from the issue "
                                     f"branch. Why: {run.stop_why}")
            note = f"; closed PR #{pr} so the next run resumes from the branch"
        ctx.tracker.add_labels(number, [scheduler.READY])
        removals = [scheduler.BLOCKED, scheduler.NEEDS_HUMAN, scheduler.RUNNING]
    for label in removals:  # One label per call: a failure leaves the rest for the retry.
        ctx.tracker.remove_labels(number, [label])
    if issue.state == "open":
        scheduler.board.sync_status(ctx, number, "Ready")
    drop_stop_pending(ctx.store, number)
    return note


def recover_pending_stops(ctx: TickContext, report: TickReport) -> None:
    """Drive every recorded stop to its end from the local record, whatever the labels say.

    No stop runs while a tick holds the lock. A record on a run that ended for
    its own reason is dropped (that run's outcome owns its labels); a stopped run
    is completed. A run still recorded as running is left to _recover_runs.
    """
    from foragent.run_publish import STOPPED, stop_kept_work

    directory = ctx.store.root / "stops"
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        record = json.loads(path.read_text(encoding="utf-8"))
        number = record["number"]
        run = ctx.store.get_run(number)
        if run is None or run.started_at != record["started_at"]:
            drop_stop_pending(ctx.store, number)  # A newer run replaced the stopped one.
        elif run.status == "running":
            continue  # _recover_runs: a live runner's abandoned intent is cleared, a dead one finishes stopped.
        elif run.outcome == STOPPED and stop_kept_work(run):
            try:
                report.actions.append(f"#{number} completed a pending stop{complete_stop(ctx, run)}")
            except Exception as exc:
                report.actions.append(f"#{number} stop still pending: {' '.join(str(exc).split())}")
        else:
            drop_stop_pending(ctx.store, number)


def clear_stale_stop(ctx: TickContext, run: Run) -> Run:
    """Drop a stop intent left on a live run by a `fa orch stop` that died before signaling.

    The stop holds the tick lock from its intent to its labels, so a tick that
    sees an intent on a live runner knows no stop is in progress. Left in place,
    the intent would silence the runner's own flags and end it as stopped.
    """
    if not run.stop_why:
        return run
    from foragent.run_publish import STOPPED

    drop_stop_pending(ctx.store, run.number)
    scheduler.append_event(ctx.store, ctx.now(), run.number, "stop-abandoned",
                           f"#{run.number}: a stop ended before signaling the runner; why: {run.stop_why}")
    return ctx.store.update_run(run.number, stop_why="", **({"outcome": None} if run.outcome == STOPPED else {}))

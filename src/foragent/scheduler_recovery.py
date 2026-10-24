"""Stop runners and recover crashed, stalled or orphaned runs."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import board, scheduler
from foragent.capacity import LAUNCH_GRACE
from foragent.labels import BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane
from foragent.orch_events import append_event
from foragent.reasons import Reason
from foragent.tracker import Issue, local_issue_models

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

def _kill_executor(ctx: TickContext, number: int) -> None:
    # Re-read after stopping the runner: it may have spawned or reaped an executor.
    run = ctx.store.get_run(number)
    if run.executor_pgid is not None:
        ctx.kill_runner(run.executor_pgid)
        ctx.store.update_run(number, executor_pgid=None)


def _remove_run_worktree(
    ctx: TickContext, number: int, worktree: Path, report: TickReport, *, outcome: str,
) -> None:
    try:
        if worktree.exists() and outcome != "merged":
            try:
                scheduler._preserve_failed_implementation(
                    number, ctx, ctx.git.at(worktree), outcome, approval=ctx.store.load_approval(number),
                    tracker_available=ctx.store.github_wait_until(ctx.now()) is None,
                )
            except Exception as exc:
                detail = f"WIP preservation failed before worktree cleanup: {exc}"
                scheduler._append(ctx.store.log_path(number), detail)
                append_event(ctx.store, ctx.now(), number, "wip-preservation-failed", detail)
                report.actions.append(f"#{number} {detail}")
    finally:
        ctx.git.remove_worktree(worktree)


def _kill_run(
    ctx: TickContext, number: int, report: TickReport, *, outcome: str = "killed",
    count_stall: bool = False, detail: str = "",
) -> None:
    run = ctx.store.get_run(number)
    if run is None or run.status != "running":
        return
    if count_stall:
        ctx.store.update_run(number, restarts=run.restarts + 1, outcome=outcome)
    try:
        if run.pid is not None:
            ctx.kill_runner(run.pid)
        scheduler._kill_executor(ctx, number)
    except Exception:
        if count_stall:
            ctx.store.update_run(number, restarts=run.restarts, outcome=run.outcome)
        raise
    scheduler._remove_run_worktree(ctx, number, Path(run.worktree), report, outcome=outcome)
    ctx.store.finish_run(number, outcome=outcome, detail=detail or "Runner stopped by the scheduler.")
    report.actions.append(f"#{number} runner killed")


def _crash_detail(ctx: TickContext, number: int) -> str:
    # The label generation makes retries after tracker failures idempotent.
    generation = sum(
        event.label == RUNNING and event.action == "labeled"
        for event in ctx.tracker.label_events(number)
    )
    return f"Crash recovery for running label generation {generation}."


def _local_issue(ctx: TickContext, number: int) -> Issue | None:
    """Read newer runner writes; a missing row supplies no update to caller facts."""
    return next((issue for issue in local_issue_models(ctx.store) if issue.number == number), None)


def _restore_after_crash(ctx: TickContext, issue: Issue, report: TickReport) -> None:
    number = issue.number
    issue = scheduler._local_issue(ctx, number) or issue
    if RUNNING not in issue.labels:
        return
    if issue.state != "open" or issue.labels & {BLOCKED, NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [RUNNING])
        return
    if ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, Reason.RETRIES_EXCEEDED,
             "Runner stopped with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged retries-exceeded after runner crash with open PR")
        append_event(ctx.store, ctx.now(), number, "retries-exceeded",
                     "Runner crashed with an open PR; blocked for review or retry.")
        return
    failures = 0
    for past in reversed(ctx.store.read_history()):
        if past.number != number:
            continue
        if past.outcome != "crashed":
            break
        failures += 1
    if failures >= 2:
        scheduler.flag(
            ctx, number, Reason.RETRIES_EXCEEDED,
            "Runner crashed twice consecutively for this issue. Investigate before resuming.",
        )
        report.actions.append(f"#{number} flagged retries-exceeded")
        append_event(ctx.store, ctx.now(), number, "retries-exceeded",
                     "Runner crashed twice consecutively; blocked for investigation.")
    else:
        ctx.tracker.add_labels(number, [READY])
        ctx.tracker.remove_labels(number, [RUNNING])
        report.actions.append(f"#{number} returned to ready after runner crash")
        board.sync_status(ctx, number, "Ready")
        append_event(ctx.store, ctx.now(), number, "crash-restart",
                     "Runner crashed; returned to ready for restart.")


def _recover_orphan(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    issue = scheduler._local_issue(ctx, issue.number) or issue
    run = ctx.store.get_run(issue.number)
    if ctx.store.run_start_pending(issue.number):
        if run is None or run.status != "running":
            # The label request may have succeeded even when its reply was lost.
            # No run record means this scheduler never reached the launcher.
            ctx.tracker.remove_labels(issue.number, [RUNNING])
            ctx.store.clear_run_start(issue.number)
            report.actions.append(f"#{issue.number} incomplete start recovered before launch")
            return ctx.tracker.get_issue(issue.number)
        ctx.store.clear_run_start(issue.number)
    if RUNNING not in issue.labels or (run is not None and run.status == "running"):
        return issue
    scheduler._remove_run_worktree(
        ctx, issue.number, Path(run.worktree) if run is not None else ctx.store.worktree_path(issue.number),
        report, outcome=(run.outcome or "crashed") if run is not None else "crashed",
    )
    if run is not None and run.outcome == "merged":
        ctx.tracker.remove_labels(issue.number, [READY, RUNNING])
        return ctx.tracker.get_issue(issue.number)
    if run is not None and run.outcome == "limit-wait":
        if not issue.labels & {BLOCKED, NEEDS_HUMAN}:
            ctx.tracker.add_labels(issue.number, [READY])
        ctx.tracker.remove_labels(issue.number, [RUNNING])
        return ctx.tracker.get_issue(issue.number)
    if run is not None and run.outcome in {"stalled-restart", "stalled"}:
        scheduler._restore_stalled(ctx, issue.number, run.restarts, report)
        return ctx.tracker.get_issue(issue.number)
    detail = scheduler._crash_detail(ctx, issue.number)
    if run is None or run.outcome != "crashed" or run.detail != detail:
        ctx.store.start_run(
            issue.number, branch=f"foragent/issue-{issue.number}",
            worktree=ctx.store.worktree_path(issue.number),
            lane=issue_lane(issue.labels, ctx.config.lanes),
        )
        ctx.store.finish_run(issue.number, outcome="crashed", detail=detail)
    report.actions.append(f"#{issue.number} orphaned running label recovered as crashed")
    scheduler._restore_after_crash(ctx, issue, report)
    return ctx.tracker.get_issue(issue.number)


def _restore_stalled(ctx: TickContext, number: int, restarts: int, report: TickReport) -> None:
    issue = ctx.tracker.get_issue(number)
    if issue.state != "open" or issue.labels & {BLOCKED, NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [RUNNING])
    elif ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, Reason.STALLED,
             "Runner stalled with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged stalled with open PR")
        append_event(ctx.store, ctx.now(), number, "stalled",
                     "Runner stalled with an open PR; blocked for review or retry.")
    elif restarts <= 1:
        ctx.tracker.add_labels(number, [READY])
        ctx.tracker.remove_labels(number, [RUNNING])
        report.actions.append(f"#{number} returned to ready after stall (restart {restarts})")
        board.sync_status(ctx, number, "Ready")
        append_event(ctx.store, ctx.now(), number, "stall-restart",
                     "Runner stalled; stopped and returned to ready for restart.")
    else:
        scheduler.flag(ctx, number, Reason.STALLED, "Runner stalled again after one restart.")
        report.actions.append(f"#{number} flagged stalled")
        append_event(ctx.store, ctx.now(), number, "stalled",
                     "Runner stalled again after one restart; stopped and blocked.")


def _recover_runs(ctx: TickContext, report: TickReport) -> None:
    for run in ctx.store.active_runs():
        if run.pid is None:
            dead = (
                ctx.now() - datetime.fromisoformat(run.started_at) >= LAUNCH_GRACE
                and not ctx.runner_process_exists(run.number)
            )
        else:
            try:
                os.kill(run.pid, 0)
            except ProcessLookupError:
                dead = True
            except PermissionError:
                dead = False
            else:
                dead = False
        # The runner may have finished while another run or its process was checked.
        run = ctx.store.get_run(run.number)
        if run is None or run.status != "running":
            continue
        if not dead:
            waiting = run.waiting_until is not None and ctx.now() < datetime.fromisoformat(run.waiting_until)
            runtime = (ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds() - run.waited_seconds
            if run.pid is not None and not waiting and runtime > (ctx.config.max_runtime_minutes + 10) * 60:
                scheduler._kill_run(ctx, run.number, report, detail="Runner exceeded its runtime limit.")
                scheduler.flag(ctx, run.number, Reason.TIMEOUT, "Too big for one session; split it in order")
                report.actions.append(f"#{run.number} flagged timeout")
            elif run.pid is not None and not waiting and ctx.now() - datetime.fromisoformat(
                run.last_progress_at or run.started_at,
            ) > timedelta(minutes=ctx.config.stall_minutes):
                restarts = run.restarts + 1
                outcome = "stalled-restart" if restarts <= 1 else "stalled"
                scheduler._kill_run(ctx, run.number, report, outcome=outcome, count_stall=True,
                          detail="Runner stopped making progress past the stall limit.")
                scheduler._restore_stalled(ctx, run.number, restarts, report)
            continue
        # Clean up a crashed group or finish an interrupted stall termination.
        # A recorded completion does not need another signal.
        if run.pid is not None and run.outcome in {None, "crashed", "stalled-restart", "stalled"}:
            ctx.kill_runner(run.pid)
        scheduler._kill_executor(ctx, run.number)
        outcome = run.outcome or "crashed"
        scheduler._remove_run_worktree(ctx, run.number, Path(run.worktree), report, outcome=outcome)
        detail = scheduler._crash_detail(ctx, run.number) if outcome == "crashed" else run.detail
        finished = ctx.store.finish_run(run.number, outcome=outcome, detail=detail)
        if finished.outcome in {"stalled-restart", "stalled"}:
            scheduler._restore_stalled(ctx, run.number, finished.restarts, report)
            continue
        if finished.outcome != "crashed":
            continue
        report.actions.append(f"#{run.number} runner crashed")
        issue = ctx.tracker.get_issue(run.number)
        scheduler._restore_after_crash(ctx, issue, report)

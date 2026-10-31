"""Stop runners and recover crashed, stalled or orphaned runs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foragent import orch_scope, processes, scheduler, scheduler_reconcile, sleep_credit
from foragent.scheduler_stop import (
    DECISION_FIELDS, LIVENESS_FIELDS, _flag_stop_refused, _stop_unconfirmed, _restore_stopped, _pid_exists, _run_processes_exist,
    _stop_already_refused, _stop_run_processes, registered_since, runner_alive,
)
from foragent.state import Run
from foragent.tracker import Issue

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _kill_executor(ctx: TickContext, number: int | str) -> None:
    run = ctx.store.get_run(number)  # Re-read after stopping the runner: it may have spawned or reaped an executor.
    if run.executor_pgid is not None:
        with processes.recorded_tree(run.process_identities):
            ctx.kill_runner(run.executor_pgid)
        if not _stop_unconfirmed(run):  # A surviving executor stays recorded, so the next tick still sees it.
            ctx.store.update_run(number, executor_pgid=None)


def _remove_run_worktree(
    ctx: TickContext, number: int | str, worktree: Path, report: TickReport, *, outcome: str, why: str = "",
) -> None:  # `why`: `fa orch stop`'s reason, named in the WIP; its killed tree owns its deletions.
    try:
        if worktree.exists() and outcome != "merged" and isinstance(number, int):
            try:
                scheduler._preserve_failed_implementation(
                    number, ctx, ctx.git.at(worktree), f"{outcome}: {why}" if why else outcome, approval=ctx.store.load_approval(number),
                    tracker_available=ctx.store.github_wait_until(ctx.now()) is None and orch_scope.shares(ctx, number),
                    owned_deletions=bool(why),
                )
            except Exception as exc:
                detail = f"WIP preservation failed before worktree cleanup: {exc}"
                scheduler._append(ctx.store.log_path(number), detail)
                scheduler.append_event(ctx.store, ctx.now(), number, "wip-preservation-failed", detail)
                report.actions.append(f"#{number} {detail}")
    finally:
        ctx.git.remove_worktree(worktree)


def _kill_run(
    ctx: TickContext, number: int | str, report: TickReport, *, outcome: str = "killed",
    count_stall: bool = False, detail: str = "", decided: Run | None = None, unless_talking: bool = False,
) -> bool:
    run = ctx.store.get_run(number)
    if run is None or run.status != "running":
        return False
    if decided is not None and any(getattr(run, name) != getattr(decided, name) for name in DECISION_FIELDS):
        return False  # The runner recorded progress or a wait after the limit check: decide again next tick.
    mark = (lambda: ctx.store.update_run(number, restarts=run.restarts + 1, outcome=outcome)) if count_stall else None
    try:  # A timed stop writes its stall mark only under worktree_lock, after reading the talk again.
        stopped = (scheduler.lane_talk.guarded_stop(ctx, number, report, lambda c: _stop_run_processes(c, run, report), mark)
                   if unless_talking else (mark and mark(), _stop_run_processes(ctx, run, report))[1])
    except Exception:
        if count_stall:
            ctx.store.update_run(number, restarts=run.restarts, outcome=run.outcome)
        raise
    if stopped not in {"stopped", "gone" if outcome == "stopped" else "stopped"} or registered_since(ctx, run):  # Fresh read.
        if count_stall and stopped != "skipped":  # A skipped stop wrote nothing to undo.
            ctx.store.update_run(number, restarts=run.restarts, outcome=run.outcome)
        if stopped == "gone":
            _finish_dead_run(ctx, run, report)
        return False
    scheduler._remove_run_worktree(ctx, number, scheduler.Path(run.worktree), report, outcome=outcome, why=detail if outcome == "stopped" else "")
    ctx.store.finish_run(number, outcome=outcome, detail=detail or "Runner stopped by the scheduler.")
    report.actions.append(f"#{number} runner killed")
    return True


def _crash_detail(ctx: TickContext, number: int) -> str:
    # The label generation makes retries after tracker failures idempotent.
    generation = sum(
        event.label == scheduler.RUNNING and event.action == "labeled"
        for event in ctx.tracker.label_events(number)
    )
    return f"Crash recovery for running label generation {generation}."


def _local_issue(ctx: TickContext, number: int) -> Issue | None:
    """Read newer runner writes; a missing row supplies no update to caller facts."""
    return next((issue for issue in scheduler.local_issue_models(ctx.store) if issue.number == number), None)


def _restore_after_crash(ctx: TickContext, issue: Issue, report: TickReport) -> None:
    number = issue.number
    issue = scheduler._local_issue(ctx, number) or issue
    if scheduler.RUNNING not in issue.labels:
        return
    if issue.state != "open" or issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
        return
    if ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, scheduler.Reason.RETRIES_EXCEEDED,
             "Runner stopped with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged retries-exceeded after runner crash with open PR")
        scheduler.append_event(ctx.store, ctx.now(), number, "retries-exceeded",
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
            ctx, number, scheduler.Reason.RETRIES_EXCEEDED,
            "Runner crashed twice consecutively for this issue. Investigate before resuming.",
        )
        report.actions.append(f"#{number} flagged retries-exceeded")
        scheduler.append_event(ctx.store, ctx.now(), number, "retries-exceeded",
                     "Runner crashed twice consecutively; blocked for investigation.")
    else:
        ctx.tracker.add_labels(number, [scheduler.READY])
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
        report.actions.append(f"#{number} returned to ready after runner crash")
        scheduler.board.sync_status(ctx, number, "Ready")
        scheduler.append_event(ctx.store, ctx.now(), number, "crash-restart",
                     "Runner crashed; returned to ready for restart.")


def _recover_orphan(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    issue = scheduler._local_issue(ctx, issue.number) or issue
    run = ctx.store.get_run(issue.number)
    if ctx.store.run_start_pending(issue.number):
        if run is None or run.status != "running":
            # The label request may have succeeded even when its reply was lost.
            # No run record means this scheduler never reached the launcher.
            if not orch_scope.may_write(ctx, issue.number):  # Relabelled since the listing (#1004).
                ctx.store.clear_run_start(issue.number)
                return issue
            ctx.tracker.remove_labels(issue.number, [scheduler.RUNNING])
            ctx.store.clear_run_start(issue.number)
            report.actions.append(f"#{issue.number} incomplete start recovered before launch")
            return ctx.tracker.get_issue(issue.number)
        ctx.store.clear_run_start(issue.number)
    if scheduler.RUNNING not in issue.labels or (run is not None and run.status == "running"):
        return issue
    scheduler._remove_run_worktree(
        ctx, issue.number, scheduler.Path(run.worktree) if run is not None else ctx.store.worktree_path(issue.number),
        report, outcome=(run.outcome or "crashed") if run is not None else "crashed",
    )
    if not orch_scope.may_write(ctx, issue.number):
        return issue  # Local cleanup only: the labels are another person's now (#1004).
    if run is not None and run.outcome == "merged":
        ctx.tracker.remove_labels(issue.number, [scheduler.READY, scheduler.RUNNING])
        return ctx.tracker.get_issue(issue.number)
    if run is not None and run.outcome == "limit-wait":
        if not issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN}:
            ctx.tracker.add_labels(issue.number, [scheduler.READY])
        ctx.tracker.remove_labels(issue.number, [scheduler.RUNNING])
        return ctx.tracker.get_issue(issue.number)
    if run is not None and run.outcome in {"stalled-restart", "stalled", "stopped"}:  # stopped: detail is the why.
        _restore_stopped(ctx, issue.number, run.detail, report) if run.outcome == "stopped" else scheduler._restore_stalled(ctx, issue.number, run.restarts, report)
        return ctx.tracker.get_issue(issue.number)
    detail = scheduler_reconcile.orphan_crash_detail(ctx, issue.number, run)
    if run is None or run.outcome != "crashed" or run.detail != detail:
        ctx.store.start_run(
            issue.number, branch=f"foragent/issue-{issue.number}",
            worktree=ctx.store.worktree_path(issue.number),
            lane=scheduler.issue_lane(issue.labels, ctx.config.lanes),
            **({"implementer": run.implementer, "executor": run.executor, "reviewer": run.reviewer} if run else {}),  # A crash keeps its pair.
        )
        ctx.store.finish_run(issue.number, outcome="crashed", detail=detail)
    report.actions.append(f"#{issue.number} orphaned running label recovered as crashed")
    scheduler._restore_after_crash(ctx, issue, report)
    return ctx.tracker.get_issue(issue.number)


def _restore_stalled(ctx: TickContext, number: int, restarts: int, report: TickReport) -> None:
    issue = ctx.tracker.get_issue(number)
    if issue.state != "open" or issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
    elif ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        scheduler.flag(ctx, number, scheduler.Reason.STALLED,
             "Runner stalled with an open PR; rerun review or retry implementation.")
        report.actions.append(f"#{number} flagged stalled with open PR")
        scheduler.append_event(ctx.store, ctx.now(), number, "stalled",
                     "Runner stalled with an open PR; blocked for review or retry.")
    elif restarts <= 1:
        ctx.tracker.add_labels(number, [scheduler.READY])
        ctx.tracker.remove_labels(number, [scheduler.RUNNING])
        report.actions.append(f"#{number} returned to ready after stall (restart {restarts})")
        scheduler.board.sync_status(ctx, number, "Ready")
        scheduler.append_event(ctx.store, ctx.now(), number, "stall-restart",
                     "Runner stalled; stopped and returned to ready for restart.")
    else:
        scheduler.flag(ctx, number, scheduler.Reason.STALLED, "Runner stalled again after one restart.")
        report.actions.append(f"#{number} flagged stalled")
        scheduler.append_event(ctx.store, ctx.now(), number, "stalled",
                     "Runner stalled again after one restart; stopped and blocked.")


def _recover_runs(ctx: TickContext, report: TickReport) -> None:
    slept = sleep_credit.credit(ctx.store, ctx.now())  # The full tick alone credits machine sleep (#415).
    grace = sleep_credit.wake_grace(ctx.store) if slept > sleep_credit.WAKE_THRESHOLD_SECONDS else None
    if grace is not None:  # Report the stored end, not a second clock reading.
        report.actions.append(scheduler.text(
            "sleep_wake", ctx.config.language, hours=int(slept // 3600),
            minutes=int(slept % 3600 // 60), until=grace[1].astimezone().strftime("%H:%M:%S")))
    for run in ctx.store.active_runs():
        issue = run.kind == "issue"  # Dreams share this path by record key; only issues touch labels.
        snapshot = processes.current_snapshot()
        cached = snapshot.rows.get(run.pid) if snapshot is not None and snapshot.available else None
        if cached is not None and any(
            item["pid"] == run.pid and item["start_time"] == cached.start_time
            for item in run.process_identities
        ):
            run = ctx.store.update_run(run.key, routine=True, observe=True)
            dead = False  # Routine positive liveness needs no further enumeration.
        else:
            # Keep witnessed descendants across subsequent reparenting or runner death.
            run = ctx.store.update_run(run.key, observe=True)
            if run.pid is None:
                dead = (
                    ctx.now() - scheduler.datetime.fromisoformat(run.started_at) >= scheduler.LAUNCH_GRACE
                    and not (issue and ctx.runner_process_exists(run.number))
                )
            else:
                dead = runner_alive(run, _pid_exists) is False  # Only confirmed death; unknown waits.
        # The runner may have finished, or registered and progressed, while its process was checked.
        decided, run = run, ctx.store.get_run(run.key)
        if run is None or run.status != "running" or dead and any(
                getattr(run, name) != getattr(decided, name) for name in LIVENESS_FIELDS):
            continue
        if not dead:
            if run.talking and scheduler.lane_talk.tick_talk(ctx, run, report) or _stop_already_refused(ctx, run):
                continue  # The talk is probed first, even past a refused stop; only a confirmed closure ends it.
            waiting = run.waiting_until is not None and ctx.now() < scheduler.datetime.fromisoformat(run.waiting_until)
            runtime = ((ctx.now() - scheduler.datetime.fromisoformat(run.started_at)).total_seconds()
                       - run.waited_seconds - run.slept_seconds - run.talk_pause_seconds(ctx.now()))
            if run.pid is not None and not waiting and runtime > (ctx.config.max_runtime_minutes + 10) * 60:
                if scheduler._kill_run(ctx, run.key, report, outcome="killed" if issue else "error", unless_talking=True,
                                       detail="Runner exceeded its runtime limit.", decided=run) and issue \
                        and orch_scope.shares(ctx, run.number):  # An owner edit keeps cleanup local (#1004).
                    scheduler.flag(ctx, run.number, scheduler.Reason.TIMEOUT, "Too big for one session; split it in order")
                    report.actions.append(f"#{run.number} flagged timeout")
            elif run.pid is not None and not waiting and not sleep_credit.in_wake_grace(
                    ctx.store, run, ctx.now()) and ctx.now() - scheduler.lane_talk.stall_since(
                ctx.store, run,  # The later of the last progress and the latest talk closing (D95).
            ) - scheduler.timedelta(seconds=run.slept_since_progress) > scheduler.timedelta(
                minutes=ctx.config.stall_minutes):
                restarts = run.restarts + 1
                outcome = ("stalled-restart" if restarts <= 1 else "stalled") if issue else "error"
                if scheduler._kill_run(ctx, run.key, report, outcome=outcome, count_stall=True, decided=run,
                          detail="Runner stopped making progress past the stall limit.", unless_talking=True) and issue \
                        and orch_scope.shares(ctx, run.number):
                    scheduler._restore_stalled(ctx, run.number, restarts, report)
            continue
        # Clean up a crashed group or finish an interrupted stall; a recorded completion needs no signal.
        stopped = _stop_run_processes(
            ctx, run, report, stop_runner=run.outcome in {None, "crashed", "stalled-restart", "stalled"},
        )
        if stopped != "refused":
            _finish_dead_run(ctx, run, report)


def _finish_dead_run(ctx: TickContext, run: Run, report: TickReport) -> None:
    if registered_since(ctx, run):  # The runner registered or progressed during the stop.
        return
    outcome = (run.outcome or "crashed") if run.kind == "issue" else "error"  # A dead dream: error, no labels.
    if run.executor_pgid is not None:
        ctx.store.update_run(run.key, executor_pgid=None)
    scheduler._remove_run_worktree(ctx, run.key, scheduler.Path(run.worktree), report, outcome=outcome)
    detail = scheduler._crash_detail(ctx, run.number) if outcome == "crashed" else run.detail
    finished = ctx.store.finish_run(run.key, outcome=outcome, detail=detail)
    if run.kind == "issue" and not orch_scope.shares(ctx, run.number):
        report.actions.append(f"#{run.number} runner cleaned up locally; the issue belongs to another factory")
    elif finished.outcome in {"stalled-restart", "stalled"}:
        scheduler._restore_stalled(ctx, run.number, finished.restarts, report)
    elif finished.outcome == "crashed":
        report.actions.append(f"#{run.number} runner crashed")
        issue = ctx.tracker.get_issue(run.number)
        scheduler._restore_after_crash(ctx, issue, report)


def _reap_inactive_runs(ctx: TickContext, report: TickReport) -> None:
    """Only recorded ownership authorizes cleanup, including closed issues and finished dreams."""
    for run in [*ctx.store.read_history(), *ctx.store.dream_runs()]:  # Dreams stay out of runs.jsonl.
        current = ctx.store.get_run(run.key)
        if current is not None and current.status == "running":
            continue
        if not run.process_identities:
            continue
        if _stop_already_refused(ctx, run):
            continue
        try:
            with processes.recorded_tree(run.process_identities):
                count = processes.kill_process_group(run.pid or run.executor_pgid or run.process_identities[0]["pid"])
        except (ValueError, PermissionError, TimeoutError) as exc:
            try:
                if not _run_processes_exist(run):
                    continue
            except PermissionError:
                pass
            _flag_stop_refused(ctx, run, report, exc)
            continue
        if count:
            summary = f"Reaped {count} owned processes from inactive run."
            scheduler.append_event(ctx.store, ctx.now(), run.number if run.kind == "issue" else None, "orphans-reaped", summary)
            report.actions.append(f"#{run.key} {summary}")

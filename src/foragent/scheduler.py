"""Promote approved issues and launch runners within lane and project budgets."""

from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from fnmatch import translate
from glob import has_magic
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time

from foragent import board, board_parents, gh, lane_talk, launchd, orch_dream, orch_github, orch_plan, orch_push, orch_queue, orch_role_raise, orch_spawn, orch_waits, scheduler_housekeeping, tidy, usage_recheck, watch_open
from foragent.actions import flag, invalidate_approval
from foragent.config import Config, in_run_hours
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, issue_size, lane_order
from foragent.orch_dream import DREAM_EVERY_MERGES, dream_due  # noqa: F401
from foragent.orch_events import append_event
from foragent.reasons import Reason, format_approval_comment, format_comment
from foragent.processes import current_snapshot, kill_process_group, runner_commands_exist
from foragent.scheduler_stop import runner_process_exists, tick_processes
from foragent.runner import _append, _batch_pr, _preserve_failed_implementation
from foragent.scheduler_start import (
    INCREASE_INTERVAL, INITIAL_PARALLEL, LAUNCH_GRACE, _apply_budgets, _github_pause, _github_wait, _has_slot,
    _machine_budget, _reopen_view, _reserve_slot, _start_ready, append_budget_history, append_memory_hold,
    decrease_allowed, machine_load, machine_lock, machine_runs, next_parallel, prebuild_acceptance_baseline,
    read_state, start_baseline_thread, step, timed, update_test_slots, write_state, claude_allowed, choose_executor,
)
from foragent.state import Approval, StateStore
from foragent.tracker import Comment, Dependency, Issue, LabelEvent, PullRequest, RecordingTracker, Tracker, TrackerError, local_issue_models

TICK_JITTER = timedelta(milliseconds=500)
OPTIONAL_GH_SECONDS = 10  # Batch reconciliation and parent following together, per tick.
GH_CALL_SECONDS = 5  # One gh subprocess in those steps, or the fallback issue read before a start.
BUSY_TICK = "another tick is running"


@dataclass
class TickContext:
    repo: str
    repo_root: Path
    tracker: Tracker
    store: StateStore
    config: Config
    git: GitRepo
    now: Callable[[], datetime]
    launcher: Callable[..., int | None]  # (number) for an issue; (kind="dream", lane=...) for a dream.
    start_runs: bool = True
    runner_process_exists: Callable[[int], bool] = runner_process_exists
    kill_runner: Callable[[int], None] = kill_process_group
    baseline_launcher: Callable = start_baseline_thread
    committed_config: bool = False  # fa gate tick: settings from origin/<base> after the fetch.
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.tracker = RecordingTracker(self.tracker, self, queue_writes=True)


@dataclass
class TickReport:
    skipped: str | None = None
    actions: list[str] = field(default_factory=list)


# Facade bindings for callers and monkeypatches; split modules call each other through here.
from foragent.scheduler_approval import _check_integrity, _invalidate_approval, _latest_approval, _snapshot  # noqa: E402
from foragent.scheduler_recovery import (  # noqa: E402
    _crash_detail, _kill_executor, _kill_run, _local_issue, _recover_orphan, _recover_runs, _remove_run_worktree,
    _restore_after_crash, _restore_stalled, _reap_inactive_runs,
)
from foragent.scheduler_checks import (  # noqa: E402
    _blockers, _check_prerequisites, _confirmed_read, _escalate_overdue_blocker, _label_events, _open_pr,
    _reconcile_batches,
)
from foragent.scheduler_config import fetch_base  # noqa: E402
from foragent.scheduler_lanes import (  # noqa: E402
    _can_start_in_lane, _file_patterns_overlap, _filesystem_case_sensitive,
)


def tick(ctx: TickContext) -> TickReport:
    with ctx.store.tick_lock() as acquired, orch_spawn.deciding(ctx, acquired) as keep:
        if not acquired:  # The tick that holds the lock renews the gate heartbeat and decides D76.
            return TickReport(skipped=BUSY_TICK)
        with scheduler_housekeeping.after_tick(ctx) as done:  # Log pruning on every return path.
            timed("gate_heartbeat", ctx.store.write_heartbeat, "gate", ctx.now())
            timed("launchd_refresh", launchd.refresh_from_tick, ctx.repo, ctx.repo_root, ctx.store.root)
            with tick_processes(ctx.store) as snapshot:
                cleanup, ctx._budget_sample = TickReport(), None  # machine.yml applies once in every tick.
                if snapshot.available:
                    with step("reap_inactive_runs"):
                        _reap_inactive_runs(ctx, cleanup)
                timed("usage_recheck", usage_recheck.recheck, ctx)  # Free usage reads, also during a GitHub wait; frees this tick's starts.
                timed("plan_expiry", orch_plan.record_expiry, ctx.store, ctx.now())  # Local journal, also during a GitHub wait.
                if skipped := _github_wait(ctx, {}, "github_wait_check"):
                    return keep(done(TickReport(skipped=skipped, actions=cleanup.actions)))
                if not isinstance(ctx.tracker, RecordingTracker):
                    ctx.tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
                try:
                    report = _tick_locked(ctx)
                    report.actions[:0] = cleanup.actions
                except TrackerError as exc:
                    if not exc.is_rate_limit:
                        raise
                    return keep(done(TickReport(skipped=_github_pause(ctx, exc), actions=cleanup.actions)))
                orch_role_raise.tick(ctx.store, ctx.repo_root, ctx.now(), ctx.config.language)  # Met role signals (#794).
                return keep(done(report, finished=True))  # Only a finished tick checks tidy.


def _tick_locked(ctx: TickContext) -> TickReport:
    """Run one tick's steps; after a failed refresh every gh subprocess is capped (see _tick_steps)."""
    with ExitStack() as fallback:
        ctx._fallback = fallback
        return _tick_steps(ctx)


def _tick_steps(ctx: TickContext) -> TickReport:
    report, ctx._budget_sample = TickReport(), None
    started = ctx.now()  # The tick's own time; the fetch below can take seconds.
    timed("fetch_base", fetch_base, ctx)  # Also applies origin's committed .foragent.yml for fa gate tick.
    base_ref = f"origin/{ctx.config.base_branch}"
    ctx._refresh_failed = False
    with step("github_refresh"):
        # A listing taken this tick can confirm remembered per-issue reads. A periodic tick starts within
        # start-up jitter of its nominal 300 s cadence; stamped with its start, it is the next refresh tick.
        listing_current = (ctx.store.issues_refresh_due(started + TICK_JITTER)
                           or bool(ctx.store.github_outbox()))
        if listing_current:
            # Keep an existing recorder; wrapping again would bypass its own state.
            recorder = ctx.tracker if isinstance(ctx.tracker, RecordingTracker) else RecordingTracker(ctx.tracker, ctx)
            try:
                changed = recorder.refresh_issues(observed_at=started)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                # The failure and the old as_of stay recorded; admission works from stored issues
                # and re-reads each one just before its start.
                ctx._refresh_failed, listing_current = True, False
                # GitHub is failing: no later gh subprocess of this tick may wait longer than one fallback read.
                ctx._fallback.enter_context(gh.bounded(float("inf"), GH_CALL_SECONDS))
                report.actions.append(text("tick_refresh_failed", ctx.config.language,
                                           error=ctx.store.issues_observation()["error"]))
            else:
                if changed:  # An unchanged listing costs no board reads (tick-github-budget).
                    board.refresh_cards(ctx, recorder)
        if not ctx._refresh_failed:  # The outbox is delivered only after a refresh.
            ctx.tracker.flush_outbox()
    if skipped := orch_push.refresh_waited(ctx, _github_wait(ctx, {}, "github_wait_after_refresh")):  # #129
        return TickReport(skipped=skipped)
    timed("waiting_resume", lambda: orch_waits.tick(ctx, report, confirmed=listing_current))
    snapshot = current_snapshot()
    processes_available = snapshot is None or snapshot.available
    # Unknown liveness defers routine recovery while the queue keeps moving.
    if processes_available:
        with step("run_recovery"):
            before = {run.number for run in ctx.store.active_runs()}
            try:
                _recover_runs(ctx, report)
            except TrackerError as exc:
                if exc.is_rate_limit or not ctx._refresh_failed:
                    raise
                report.actions.append(text("tick_step_deferred", ctx.config.language,
                                           step="run_recovery", error=_one_line(exc)))
            # Recovery read these issues from GitHub; after a failed refresh they start next tick, with one read.
            recovered = before - {run.number for run in ctx.store.active_runs()}
    else:
        recovered = set()
    optional = gh.Budget(OPTIONAL_GH_SECONDS, GH_CALL_SECONDS)  # Batch and parent gh work share it.
    with step("batch_reconcile"):
        _optional_step(ctx, report, optional, "batch_reconcile", lambda: _reconcile_batches(ctx, report))
    ready: dict[str, list[int]] = {}
    unknown: set[str] = set()

    def lane_for(issue: Issue) -> str:
        for label in sorted(issue.labels):
            if label.startswith("lane:") and label != "lane:main" and label[5:] not in ctx.config.lanes:
                if label not in unknown:
                    report.actions.append(f"Unknown lane {label}; treating as main")
                    unknown.add(label)
        return issue_lane(issue.labels, ctx.config.lanes)

    def consider(issue: Issue) -> None:
        """Sync one stored issue's card and promote it to ready when it may start."""
        if issue.state != "open":
            # A manually closed card leaves its column; known cards only.
            board.sync_status(ctx, issue, "closed", existing_only=True)
            return
        if processes_available:  # Cleans a left-over worktree; board writes skip parents.
            repaired = RUNNING in issue.labels or ctx.store.run_start_pending(issue.number)
            issue = _recover_orphan(ctx, issue, report)
            if repaired and ctx._refresh_failed and RUNNING not in issue.labels:
                return  # Its repair read GitHub; a second read before a start waits for the next tick.
        if ctx._refresh_failed and issue.number in recovered:
            return
        if issue.number in parents:
            return  # Its column follows its children, whatever its own labels say.
        issue = _escalate_overdue_blocker(ctx, issue, report)
        if issue.state != "open":
            board.sync_status(ctx, issue, "closed", existing_only=True)
            return
        lane = lane_for(issue)
        waiting_labels = issue.labels & {BLOCKED, NEEDS_HUMAN}
        previous_flag = ctx.store.load_flag(issue.number) if waiting_labels else None
        if waiting_labels and (
            previous_flag is None or previous_flag.reason != Reason.HASH_MISMATCH
        ):
            board.sync_status(ctx, issue, "Needs human" if NEEDS_HUMAN in waiting_labels else "Blocked")
            return
        if APPROVED not in issue.labels and not waiting_labels:
            # Removing approval returns a known card to Backlog.
            board.sync_status(ctx, issue, "unapproved", existing_only=True)
            if READY not in issue.labels:
                return
            # Revoked but still ready (fa orch park): the label history shows a new approval first.
        event, ignored = _latest_approval(
            _label_events(ctx, issue.number, confirmed=listing_current), ctx.config.approvers,
        )
        if ignored:
            report.actions.append(
                f"#{issue.number} ignored approved label from non-approver(s): {', '.join(sorted(ignored))}"
            )
        if event is None:
            return
        if waiting_labels:
            if datetime.fromisoformat(event.created_at) <= datetime.fromisoformat(previous_flag.flagged_at):
                return
            if ctx._refresh_failed:
                return  # A re-approval is new evidence; it is read and resumed after a successful refresh.
            # A new approval is a new generation even between full refreshes.
            issue = ctx.tracker.get_issue(issue.number)
            if issue.state != "open" or issue.is_pull_request or APPROVED not in issue.labels:
                return
            ctx.tracker.remove_labels(issue.number, waiting_labels)
            issue = replace(issue, labels=issue.labels - waiting_labels)
            report.actions.append(f"#{issue.number} resumed after re-approval")

        approval = ctx.store.load_approval(issue.number)
        if approval is None or approval.approved_at != event.created_at:
            if ctx._refresh_failed:
                return  # Missing approval evidence: the snapshot needs a read; only stored approvals start now.
            if not waiting_labels:
                issue = ctx.tracker.get_issue(issue.number)
            if issue.state != "open" or issue.is_pull_request or APPROVED not in issue.labels:
                return
            approval = _snapshot(ctx, issue, event, base_ref, report)
            if approval is None:
                return
        elif not _check_integrity(ctx, issue, approval, base_ref, report):
            return
        if RUNNING in issue.labels:
            board.sync_status(ctx, issue, "Running")
            return
        if not _check_prerequisites(ctx, issue, approval, report, confirmed=listing_current):
            return
        if READY in issue.labels:
            board.sync_status(ctx, issue, "Ready")
            ready.setdefault(lane, []).append(issue.number)
            return

        row = next(row for row in ctx.store.local_issues() if row["number"] == issue.number)
        if row.get("ready_approval") == approval.approved_at:
            return  # This approval already promoted it; the remote removal wins.
        ctx.tracker.add_labels(issue.number, [READY])
        board.sync_status(ctx, issue, "Ready")
        report.actions.append(f"#{issue.number} promoted to ready")
        ready.setdefault(lane, []).append(issue.number)

    with step("board_sync"):
        parents = board_parents.order_parents(ctx.store.local_issues())  # Columns follow children.
        for issue in local_issue_models(ctx.store):
            try:
                consider(issue)
            except TrackerError as exc:
                # After a failed refresh GitHub is known to be failing: one issue's read does not stop the others.
                if exc.is_rate_limit or not ctx._refresh_failed:
                    raise
                report.actions.append(text("tick_issue_deferred", ctx.config.language,
                                           number=issue.number, error=_one_line(exc)))
        if listing_current:  # Children change only through observed facts and this tick's writes.
            _optional_step(ctx, report, optional, "parent_sync", lambda: board_parents.follow_children(ctx, report))
    if skipped := _github_wait(ctx, ready, "github_wait_before_start"):
        report.skipped = skipped
        return report
    with step("apply_budgets"):
        sample = _apply_budgets(ctx, ready, workers=False)  # The worker budget is sized below with this sample.
    if sample[0] != "critical":  # Critical memory also holds the baseline prebuild (#497).
        timed("baseline_prebuild", prebuild_acceptance_baseline, ctx, base_ref, ctx.baseline_launcher)
    if not ctx.start_runs or not in_run_hours(ctx.now().astimezone(), ctx.config.run_hours):
        timed("machine_budget", _machine_budget, ctx, ready, sample)  # A tick that starts nothing still applies machine.yml and pressure.
        return report
    dreams = timed("dream_check", orch_dream.startable, ctx, sample)  # Worker demand like a ready issue.
    if not ready:
        orch_dream.start(ctx, dreams, timed("machine_budget", _machine_budget, ctx, ready, sample), report)
        _reopen_view(ctx, report)
        return report

    with step("start_ready"):  # A lane's local queue picks the order; startability was checked above.
        ready = {lane: orch_queue.ordered(ctx.store, lane, numbers) for lane, numbers in ready.items()}
        started = _start_ready(ctx, ready, lane_for, base_ref, report, sample)
    if started is None:
        return report
    orch_dream.start(ctx, dreams, ctx._tick_budget, report)  # Only a slot the ready issues left free.
    _reopen_view(ctx, report)
    return report


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def _optional_step(ctx: TickContext, report: TickReport, budget: gh.Budget, name: str, call: Callable[[], None]) -> None:
    """Run batch or parent work within the tick's shared gh budget; a failure defers it, not the starts.

    A GitHub rate limit still pauses the tick. The deferral stays visible in the
    report and the orchestrator journal; the next tick runs the step again.
    """
    try:
        with budget.spend():
            call()
    except TrackerError as exc:
        if exc.is_rate_limit:
            raise
        line = text("tick_step_deferred", ctx.config.language, step=name, error=_one_line(exc))
        report.actions.append(line)
        append_event(ctx.store, ctx.now(), None, "tick-step-deferred", line)

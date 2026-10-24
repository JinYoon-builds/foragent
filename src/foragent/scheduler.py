"""Promote approved issues and launch runners within lane and project budgets."""

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from fnmatch import translate
from glob import has_magic
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from foragent import board, machine_budget, orch_github, orch_queue, tidy, watch_open
from foragent.actions import flag, invalidate_approval
from foragent.config import Config, in_run_hours
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, issue_size, lane_order
from foragent.orch_events import append_event
from foragent.reasons import Reason, format_approval_comment, format_comment
from foragent.processes import kill_process_group
from foragent.runner import _append, _batch_pr, _preserve_failed_implementation
from foragent.scheduler_start import (
    INCREASE_INTERVAL, INITIAL_PARALLEL, LAUNCH_GRACE, _has_slot, _machine_budget, _reserve_slot, _start_ready,
    append_budget_history, decrease_allowed, machine_load, machine_lock, machine_runs, next_parallel,
    prebuild_acceptance_baseline, read_state, start_baseline_thread, update_test_slots, write_state,
)
from foragent.state import Approval, StateStore
from foragent.tracker import Comment, Dependency, Issue, LabelEvent, PullRequest, RecordingTracker, Tracker, TrackerError, local_issue_models

TICK_JITTER = timedelta(milliseconds=500)
BUSY_TICK = "another tick is running"


def runner_process_exists(number: int) -> bool:
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid,command"], check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return True  # An unavailable process list cannot establish runner death.
    pattern = re.compile(
        rf"(?:^|\s)(?:\S*/)?(?:foragent|fa|foragent\.cli)\s+gate\s+run-issue\s+{number}(?=\s|$)"
    )
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[0].isdigit() and pattern.search(fields[1]):
            return True
    return False


@dataclass
class TickContext:
    repo: str
    repo_root: Path
    tracker: Tracker
    store: StateStore
    config: Config
    git: GitRepo
    now: Callable[[], datetime]
    launcher: Callable[[int], int | None]
    start_runs: bool = True
    runner_process_exists: Callable[[int], bool] = runner_process_exists
    kill_runner: Callable[[int], None] = kill_process_group
    baseline_launcher: Callable = start_baseline_thread

    def __post_init__(self) -> None:
        self.tracker = RecordingTracker(self.tracker, self, queue_writes=True)


@dataclass
class TickReport:
    skipped: str | None = None
    actions: list[str] = field(default_factory=list)



# Preserve the original facade bindings for callers and monkeypatches.
# Split modules call each other through this module, so patches here reach them.
from foragent.scheduler_approval import _check_integrity, _invalidate_approval, _latest_approval, _snapshot  # noqa: E402
from foragent.scheduler_recovery import (  # noqa: E402
    _crash_detail, _kill_executor, _kill_run, _local_issue, _recover_orphan, _recover_runs, _remove_run_worktree,
    _restore_after_crash, _restore_stalled, _reap_inactive_runs,
)
from foragent.scheduler_checks import (  # noqa: E402
    _blockers, _check_prerequisites, _confirmed_read, _escalate_overdue_blocker, _label_events, _open_pr,
    _reconcile_batches,
)
from foragent.scheduler_lanes import (  # noqa: E402
    _can_start_in_lane, _file_patterns_overlap, _filesystem_case_sensitive,
)


def tick(ctx: TickContext) -> TickReport:
    with ctx.store.tick_lock() as acquired:
        if not acquired:
            return TickReport(skipped=BUSY_TICK)
        cleanup = TickReport()
        _reap_inactive_runs(ctx, cleanup)
        if until := ctx.store.github_wait_until(ctx.now()):
            return TickReport(skipped=f"GitHub rate limit until {until.isoformat()}", actions=cleanup.actions)
        if not isinstance(ctx.tracker, RecordingTracker):
            ctx.tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
        try:
            report = _tick_locked(ctx)
            report.actions[:0] = cleanup.actions
        except TrackerError as exc:
            if not exc.is_rate_limit:
                raise
            until = orch_github.pause(ctx, exc)
            return TickReport(skipped=f"GitHub rate limit until {until.isoformat()}", actions=cleanup.actions)
        # After this tick's starts, so idle lanes are really idle. A due tidy is a
        # request for the orchestrator's judgment, never a launch or an order.
        if request := tidy.post_if_due(ctx.store):
            report.actions.append(f"Tidy due; inbox request {request['id']}")
        return report


def _tick_locked(ctx: TickContext) -> TickReport:
    report = TickReport()
    started = ctx.now()  # The tick's own time; the fetch below can take seconds.
    ctx.git.fetch("origin", ctx.config.base_branch)
    base_ref = f"origin/{ctx.config.base_branch}"
    # A listing taken this tick can confirm remembered per-issue reads.
    # A periodic tick starts within process start-up jitter of its nominal 300 s
    # cadence, and its refresh is stamped with its start; it is the next refresh tick.
    listing_current = (ctx.store.issues_refresh_due(started + TICK_JITTER)
                       or bool(ctx.store.github_outbox()))
    if listing_current:
        # Keep an existing recorder; wrapping again would bypass its own state.
        recorder = ctx.tracker if isinstance(ctx.tracker, RecordingTracker) else RecordingTracker(ctx.tracker, ctx)
        recorder.refresh_issues(observed_at=started)
        board.refresh_cards(ctx, recorder)
    ctx.tracker.flush_outbox()
    if until := ctx.store.github_wait_until(ctx.now()):
        return TickReport(skipped=f"GitHub rate limit until {until.isoformat()}")
    _recover_runs(ctx, report)
    _reconcile_batches(ctx, report)
    ready: dict[str, list[int]] = {}
    unknown: set[str] = set()

    def lane_for(issue: Issue) -> str:
        for label in sorted(issue.labels):
            if label.startswith("lane:") and label != "lane:main" and label[5:] not in ctx.config.lanes:
                if label not in unknown:
                    report.actions.append(f"Unknown lane {label}; treating as main")
                    unknown.add(label)
        return issue_lane(issue.labels, ctx.config.lanes)

    for issue in local_issue_models(ctx.store):
        if issue.state != "open":
            # A manually closed card leaves its column; known cards only.
            board.sync_status(ctx, issue, "closed", existing_only=True)
            continue
        issue = _recover_orphan(ctx, issue, report)
        issue = _escalate_overdue_blocker(ctx, issue, report)
        if issue.state != "open":
            board.sync_status(ctx, issue, "closed", existing_only=True)
            continue
        lane = lane_for(issue)
        waiting_labels = issue.labels & {BLOCKED, NEEDS_HUMAN}
        previous_flag = ctx.store.load_flag(issue.number) if waiting_labels else None
        if waiting_labels and (
            previous_flag is None or previous_flag.reason != Reason.HASH_MISMATCH
        ):
            board.sync_status(ctx, issue, "Needs human" if NEEDS_HUMAN in waiting_labels else "Blocked")
            continue
        if APPROVED not in issue.labels and not waiting_labels:
            # Removing approval returns a known card to Backlog.
            board.sync_status(ctx, issue, "unapproved", existing_only=True)
            continue
        event, ignored = _latest_approval(
            _label_events(ctx, issue.number, confirmed=listing_current), ctx.config.approvers,
        )
        if ignored:
            report.actions.append(
                f"#{issue.number} ignored approved label from non-approver(s): {', '.join(sorted(ignored))}"
            )
        if event is None:
            continue
        if waiting_labels:
            if datetime.fromisoformat(event.created_at) <= datetime.fromisoformat(previous_flag.flagged_at):
                continue
            # A new approval is a new generation even between full refreshes.
            issue = ctx.tracker.get_issue(issue.number)
            if issue.state != "open" or issue.is_pull_request or APPROVED not in issue.labels:
                continue
            ctx.tracker.remove_labels(issue.number, waiting_labels)
            issue = replace(issue, labels=issue.labels - waiting_labels)
            report.actions.append(f"#{issue.number} resumed after re-approval")

        approval = ctx.store.load_approval(issue.number)
        if approval is None or approval.approved_at != event.created_at:
            if not waiting_labels:
                issue = ctx.tracker.get_issue(issue.number)
            if issue.state != "open" or issue.is_pull_request or APPROVED not in issue.labels:
                continue
            approval = _snapshot(ctx, issue, event, base_ref, report)
            if approval is None:
                continue
        elif not _check_integrity(ctx, issue, approval, base_ref, report):
            continue
        if RUNNING in issue.labels:
            board.sync_status(ctx, issue, "Running")
            continue
        if not _check_prerequisites(ctx, issue, approval, report, confirmed=listing_current):
            continue
        if READY in issue.labels:
            board.sync_status(ctx, issue, "Ready")
            ready.setdefault(lane, []).append(issue.number)
            continue

        row = next(row for row in ctx.store.local_issues() if row["number"] == issue.number)
        if row.get("ready_approval") == approval.approved_at:
            continue  # This approval already promoted it; the remote removal wins.
        ctx.tracker.add_labels(issue.number, [READY])
        board.sync_status(ctx, issue, "Ready")
        report.actions.append(f"#{issue.number} promoted to ready")
        ready.setdefault(lane, []).append(issue.number)

    if until := ctx.store.github_wait_until(ctx.now()):
        report.skipped = f"GitHub rate limit until {until.isoformat()}"
        return report
    # One sensor sample per tick sizes both the test slots and the run budget; machine.yml alone sizes the shared files.
    sample, machine = machine_load(), machine_budget.values(ctx.config)
    update_test_slots(
        machine["test_slots"], ctx.now(), pressure=sample[0], load_per_cpu=sample[1],
        swapins=getattr(sample, "swapins", None), floor=machine["test_slots_floor"],
    )
    prebuild_acceptance_baseline(ctx, base_ref, ctx.baseline_launcher)
    if not ctx.start_runs or not ready or not in_run_hours(ctx.now().astimezone(), ctx.config.run_hours):
        _machine_budget(ctx, ready, sample)  # A tick that starts nothing still applies machine.yml and pressure.
        return report

    # A lane's local queue picks the order; startability was already checked above.
    ready = {lane: orch_queue.ordered(ctx.store, lane, numbers) for lane, numbers in ready.items()}
    started = _start_ready(ctx, ready, lane_for, base_ref, report, sample)
    if started is None:
        return report
    if started:
        try:
            watch_open.open_view(ctx.repo_root, ctx.store, ctx.config.language)
        except Exception as exc:
            # Optional progress display must never hide successfully started runs.
            error = " ".join(str(exc).split())[:200]
            report.actions.append(text("watch_open_failed", ctx.config.language, error=error))
    return report

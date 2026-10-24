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

from foragent import board, orch_github, watch_open
from foragent.actions import flag, invalidate_approval
from foragent.capacity import (
    INCREASE_INTERVAL, INITIAL_PARALLEL, LAUNCH_GRACE, append_budget_history, decrease_allowed,
    machine_load, machine_lock, machine_runs, next_parallel, read_state,
    update_test_slots, write_state,
)
from foragent.config import Config, in_run_hours, resolved_reviewer
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, issue_size, lane_order
from foragent.orch_events import append_event
from foragent.reasons import Reason, format_approval_comment, format_comment
from foragent.processes import kill_process_group
from foragent.runner import _append, _batch_pr, _preserve_failed_implementation
from foragent.run_baseline import prebuild_acceptance_baseline, start_baseline_thread
from foragent.state import Approval, StateStore
from foragent.tracker import Comment, Dependency, Issue, LabelEvent, PullRequest, RecordingTracker, Tracker, TrackerError, local_issue_models


TICK_JITTER = timedelta(milliseconds=500)


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


def _latest_approval(events: list[LabelEvent], approvers: list[str]) -> tuple[LabelEvent | None, set[str]]:
    approval = None
    ignored: set[str] = set()
    allowed = {actor.casefold() for actor in approvers}
    for event in sorted(events, key=lambda event: datetime.fromisoformat(event.created_at)):
        if event.label != APPROVED:
            continue
        if event.action == "unlabeled":
            approval = None
            ignored.clear()
        elif event.actor.casefold() in allowed:
            approval = event
        else:
            ignored.add(event.actor)
    return approval, ignored


def _invalidate_approval(
    ctx: TickContext, issue: Issue, summary: str, details: str | None, report: TickReport,
) -> None:
    run = ctx.store.get_run(issue.number)
    if run is not None and run.status == "running":
        _kill_run(ctx, run.number, report, detail=summary)
    invalidate_approval(ctx, issue.number, summary, details)
    report.actions.append(f"#{issue.number} flagged hash-mismatch: {summary}")


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
                _preserve_failed_implementation(
                    number, ctx, ctx.git.at(worktree), outcome, approval=ctx.store.load_approval(number),
                    tracker_available=ctx.store.github_wait_until(ctx.now()) is None,
                )
            except Exception as exc:
                detail = f"WIP preservation failed before worktree cleanup: {exc}"
                _append(ctx.store.log_path(number), detail)
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
        _kill_executor(ctx, number)
    except Exception:
        if count_stall:
            ctx.store.update_run(number, restarts=run.restarts, outcome=run.outcome)
        raise
    _remove_run_worktree(ctx, number, Path(run.worktree), report, outcome=outcome)
    ctx.store.finish_run(number, outcome=outcome, detail=detail or "Runner stopped by the scheduler.")
    report.actions.append(f"#{number} runner killed")


def _check_integrity(
    ctx: TickContext, issue: Issue, approval: Approval, base_ref: str, report: TickReport,
) -> bool:
    changed_body = body_hash(issue.body) != approval.body_sha
    current_blobs = ctx.git.blob_shas(base_ref, approval.acceptance_blobs)
    changed_paths = sorted(
        path for path in approval.acceptance_blobs
        if current_blobs.get(path, "missing") != approval.acceptance_blobs[path]
    )
    if not changed_body and not changed_paths:
        return True

    changed = []
    details = []
    if changed_body:
        changed.append("the issue body")
        details.append("Issue body changed after approval.")
    if changed_paths:
        changed.append("acceptance files")
        details.extend(
            f"Acceptance file changed: `{path}` "
            f"(approved `{approval.acceptance_blobs[path]}`, current `{current_blobs.get(path, 'missing')}`)."
            for path in changed_paths
        )
    summary = f"Approval invalidated: {' and '.join(changed)} changed after approval. Re-approve to run it."
    _invalidate_approval(ctx, issue, summary, "\n".join(details), report)
    return False


def _snapshot(
    ctx: TickContext, issue: Issue, event: LabelEvent, base_ref: str, report: TickReport,
) -> Approval | None:
    edited_at = ctx.tracker.body_last_edited_at(issue.number)
    if edited_at is not None and datetime.fromisoformat(edited_at) >= datetime.fromisoformat(event.created_at):
        _invalidate_approval(
            ctx, issue,
            "Approval invalidated: approval predates an edit to the issue body. Re-approve to run it.",
            f"Last body edit: {edited_at}; approval: {event.created_at}.", report,
        )
        return None

    parsed = parse_body(issue.body)
    paths = acceptance_paths(parsed.done_when_commands, ctx.config.acceptance_dir)
    # 알려진 한계: 커밋 시각 기준이라 승인 전에 만들어 승인 후 push한 커밋은 구분 못 함
    approved_ref = ctx.git.commit_before(base_ref, event.created_at) if paths else None
    if paths and approved_ref is None:
        _invalidate_approval(
            ctx, issue,
            "Approval invalidated: no base commit exists at the approval time. Re-approve to run it.",
            f"Cannot verify acceptance files on {base_ref} at {event.created_at}.", report,
        )
        return None
    blobs = ctx.git.blob_shas(approved_ref, paths) if approved_ref is not None else {}
    approval = Approval(issue.number, issue.body, body_hash(issue.body), blobs, event.actor, event.created_at)
    if not _check_integrity(ctx, issue, approval, base_ref, report):
        return None
    ctx.store.save_approval(
        issue.number, body=approval.body, body_sha=approval.body_sha, acceptance_blobs=blobs,
        approved_by=event.actor, approved_at=event.created_at,
    )
    ctx.tracker.comment(
        issue.number, format_approval_comment(approval.body_sha, blobs, event.actor, event.created_at),
    )
    report.actions.append(f"#{issue.number} approval snapshot saved")
    board.sync_status(ctx, issue, "Approved")
    return approval


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
    issue = _local_issue(ctx, number) or issue
    if RUNNING not in issue.labels:
        return
    if issue.state != "open" or issue.labels & {BLOCKED, NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [RUNNING])
        return
    if ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        flag(ctx, number, Reason.RETRIES_EXCEEDED,
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
        flag(
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
    issue = _local_issue(ctx, issue.number) or issue
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
    _remove_run_worktree(
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
        _restore_stalled(ctx, issue.number, run.restarts, report)
        return ctx.tracker.get_issue(issue.number)
    detail = _crash_detail(ctx, issue.number)
    if run is None or run.outcome != "crashed" or run.detail != detail:
        ctx.store.start_run(
            issue.number, branch=f"foragent/issue-{issue.number}",
            worktree=ctx.store.worktree_path(issue.number),
            lane=issue_lane(issue.labels, ctx.config.lanes),
        )
        ctx.store.finish_run(issue.number, outcome="crashed", detail=detail)
    report.actions.append(f"#{issue.number} orphaned running label recovered as crashed")
    _restore_after_crash(ctx, issue, report)
    return ctx.tracker.get_issue(issue.number)


def _restore_stalled(ctx: TickContext, number: int, restarts: int, report: TickReport) -> None:
    issue = ctx.tracker.get_issue(number)
    if issue.state != "open" or issue.labels & {BLOCKED, NEEDS_HUMAN}:
        ctx.tracker.remove_labels(number, [RUNNING])
    elif ctx.tracker.find_pr(f"foragent/issue-{number}") is not None:
        flag(ctx, number, Reason.STALLED,
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
        flag(ctx, number, Reason.STALLED, "Runner stalled again after one restart.")
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
                _kill_run(ctx, run.number, report, detail="Runner exceeded its runtime limit.")
                flag(ctx, run.number, Reason.TIMEOUT, "Too big for one session; split it in order")
                report.actions.append(f"#{run.number} flagged timeout")
            elif run.pid is not None and not waiting and ctx.now() - datetime.fromisoformat(
                run.last_progress_at or run.started_at,
            ) > timedelta(minutes=ctx.config.stall_minutes):
                restarts = run.restarts + 1
                outcome = "stalled-restart" if restarts <= 1 else "stalled"
                _kill_run(ctx, run.number, report, outcome=outcome, count_stall=True,
                          detail="Runner stopped making progress past the stall limit.")
                _restore_stalled(ctx, run.number, restarts, report)
            continue
        # Clean up a crashed group or finish an interrupted stall termination.
        # A recorded completion does not need another signal.
        if run.pid is not None and run.outcome in {None, "crashed", "stalled-restart", "stalled"}:
            ctx.kill_runner(run.pid)
        _kill_executor(ctx, run.number)
        outcome = run.outcome or "crashed"
        _remove_run_worktree(ctx, run.number, Path(run.worktree), report, outcome=outcome)
        detail = _crash_detail(ctx, run.number) if outcome == "crashed" else run.detail
        finished = ctx.store.finish_run(run.number, outcome=outcome, detail=detail)
        if finished.outcome in {"stalled-restart", "stalled"}:
            _restore_stalled(ctx, run.number, finished.restarts, report)
            continue
        if finished.outcome != "crashed":
            continue
        report.actions.append(f"#{run.number} runner crashed")
        issue = ctx.tracker.get_issue(run.number)
        _restore_after_crash(ctx, issue, report)


def _confirmed_read(
    ctx: TickContext, number: int, name: str, read: Callable[[], object], *, confirmed: bool,
    links: Callable[[object], list[int | str]] = lambda value: [],
):
    """Read GitHub unless this tick's fresh listing confirmed the remembered read unchanged.

    Only a listing taken this tick is current enough to confirm a read; between
    refreshes the remote may have changed without a sign, so the read is live.
    Every live read is remembered so that the next refresh can confirm it.
    """
    if confirmed and (hit := ctx.store.confirmed_read(number, name)) is not None:
        return hit[0]
    # Facts before the read: a refresh during the read must not vouch for its value.
    baseline = ctx.store.read_baseline()
    value = read()
    ctx.store.record_read(number, name, value, baseline=baseline, links=links(value))
    return value


def _label_events(ctx: TickContext, number: int, *, confirmed: bool) -> list[LabelEvent]:
    events = _confirmed_read(
        ctx, number, "label_events", lambda: [asdict(event) for event in ctx.tracker.label_events(number)],
        confirmed=confirmed,
    )
    return [LabelEvent(**event) for event in events]


def _open_pr(ctx: TickContext, number: int, *, confirmed: bool) -> int | None:
    branch = f"foragent/issue-{number}"
    return _confirmed_read(ctx, number, "open_pr", lambda: ctx.tracker.find_pr(branch), confirmed=confirmed)


def _blockers(ctx: TickContext, number: int, *, confirmed: bool) -> list[Dependency]:
    # Each blocker is a fact link: a change to any blocker voids the remembered list.
    dependencies = _confirmed_read(
        ctx, number, "blocked_by", lambda: [asdict(dependency) for dependency in ctx.tracker.blocked_by(number)],
        confirmed=confirmed, links=lambda value: [
            dependency["number"] if dependency.get("repository") is None
            else f"{dependency['repository']}#{dependency['number']}" for dependency in value
        ],
    )
    return [Dependency(**dependency) for dependency in dependencies]


def _check_prerequisites(
    ctx: TickContext, issue: Issue, approval: Approval, report: TickReport, *, confirmed: bool = False,
) -> bool:
    latest_run = next(
        (run for run in reversed(ctx.store.read_history()) if run.number == issue.number), None,
    )
    if (
        latest_run is not None and latest_run.outcome == "merged"
        and datetime.fromisoformat(approval.approved_at) <= datetime.fromisoformat(latest_run.started_at)
    ):
        if READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [READY])
        board.sync_status(ctx, issue, "Done")
        return False
    if (
        _open_pr(ctx, issue.number, confirmed=confirmed) is not None
        and (latest_run is None or latest_run.outcome != "limit-wait")
    ):
        if READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [READY])
        board.sync_status(ctx, issue, "PR open")
        return False
    if not has_machine_check(parse_body(approval.body)):
        flag(
            ctx, issue.number, Reason.NO_MACHINE_CHECK,
            "Cannot promote: Done when has no machine check. Add an executable code block before resuming.",
        )
        report.actions.append(f"#{issue.number} flagged no-machine-check")
        return False
    dependencies = _blockers(ctx, issue.number, confirmed=confirmed)
    cancelled = next(
        (dependency for dependency in dependencies
         if dependency.state == "closed" and dependency.state_reason == "not_planned"), None,
    )
    if cancelled is not None:
        flag(
            ctx, issue.number, Reason.SPEC_QUESTION,
            f"Cannot promote: blocker #{cancelled.number} was closed as not planned. Clarify the dependency.",
        )
        report.actions.append(f"#{issue.number} flagged spec-question: blocker #{cancelled.number} was closed as not planned")
        return False
    if any(dependency.state == "open" for dependency in dependencies):
        if READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [READY])
            report.actions.append(f"#{issue.number} returned to approved waiting for open blockers")
        board.sync_status(ctx, issue, "Approved")
        return False
    return True


def tick(ctx: TickContext) -> TickReport:
    with ctx.store.tick_lock() as acquired:
        if not acquired:
            return TickReport(skipped="another tick is running")
        if until := ctx.store.github_wait_until(ctx.now()):
            return TickReport(skipped=f"GitHub rate limit until {until.isoformat()}")
        if not isinstance(ctx.tracker, RecordingTracker):
            ctx.tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
        try:
            return _tick_locked(ctx)
        except TrackerError as exc:
            if not exc.is_rate_limit:
                raise
            until = orch_github.pause(ctx, exc)
            return TickReport(skipped=f"GitHub rate limit until {until.isoformat()}")


def _escalate_overdue_blocker(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    issue = _local_issue(ctx, issue.number) or issue
    if issue.state != "open" or BLOCKED not in issue.labels or NEEDS_HUMAN in issue.labels:
        return issue
    row = next((row for row in ctx.store.local_issues() if row["number"] == issue.number), {})
    comments = [
        comment for comment in (Comment(**item) for item in row.get("comments", []))
        if re.fullmatch(
            r"<!-- foragent:blocked reason=[a-z-]+ -->",
            comment.body.partition("\n")[0].removesuffix("\r"),
        )
    ]
    latest = max(comments, key=lambda c: (datetime.fromisoformat(c.created_at), c.id), default=None)
    if latest is None or ctx.now() - datetime.fromisoformat(latest.created_at) <= timedelta(
        minutes=ctx.config.orchestrator_timeout_minutes,
    ):
        return issue
    ctx.tracker.add_labels(issue.number, [NEEDS_HUMAN])
    board.sync_status(ctx, issue, "Needs human")
    ctx.tracker.comment(issue.number, format_comment(
        Reason.ORCHESTRATOR_TIMEOUT,
        "The orchestrator has not responded to this blocker.",
        f"Blocked since {latest.created_at}; timeout: {ctx.config.orchestrator_timeout_minutes} minutes.\n\n"
        f"Latest blocker:\n{latest.body}",
    ))
    report.actions.append(f"#{issue.number} flagged orchestrator-timeout")
    append_event(ctx.store, ctx.now(), issue.number, "orchestrator-timeout",
                 "Orchestrator did not respond to the blocker in time; escalated to needs-human.")
    return replace(issue, labels=issue.labels | {NEEDS_HUMAN})


def _reconcile_batches(ctx: TickContext, report: TickReport) -> None:
    issues = {issue.number: issue for issue in local_issue_models(ctx.store) if issue.state == "open"}
    parents: set[int] = set()
    prs = [PullRequest(row["pr_number"], row["pr_head"], row["pr_base"], row["title"], row["body"])
           for row in ctx.store.local_issues() if row["merged"] and row.get("pr_head") and row.get("pr_base")]
    for pr in prs:
        child_match = re.fullmatch(r"foragent/issue-(\d+)", pr.head)
        batch_match = re.fullmatch(r"foragent/batch-(\d+)", pr.base)
        if child_match is None or batch_match is None:
            continue
        number, parent = int(child_match[1]), int(batch_match[1])
        if parent in issues:
            parents.add(parent)
        if number not in issues or ctx.tracker.parent_of(number) != parent:
            continue
        ctx.tracker.close_issue(number, f"Merged into {pr.base} (PR #{pr.number})")
        ctx.tracker.remove_labels(number, [READY, RUNNING])
        board.sync_status(ctx, number, "Done")
        del issues[number]
        report.actions.append(f"#{number} closed after batch PR #{pr.number} merged")
    for parent in sorted(parents):
        branch = f"foragent/batch-{parent}"
        if ctx.tracker.find_pr(branch) is not None:
            continue
        try:
            _batch_pr(parent, ctx)
        except (GitError, TrackerError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            report.actions.append(f"#{parent} batch PR finalization deferred: {exc}")


def _filesystem_case_sensitive(repo_root: Path) -> bool | None:
    """Probe an existing root entry without creating or modifying any files."""
    try:
        for entry in repo_root.iterdir():
            alternate = entry.with_name(entry.name.swapcase())
            if alternate == entry:
                continue
            try:
                original_stat = entry.stat()
                try:
                    alternate_stat = alternate.stat()
                except FileNotFoundError:
                    return True
            except OSError:
                continue
            return not os.path.samestat(original_stat, alternate_stat)
    except OSError:
        pass
    return None


def _file_patterns_overlap(left: str, right: str, *, case_sensitive: bool = True) -> bool:
    """Intersect path globs, including files that do not exist yet.

    As in policy._matches, * and ? stay in a component; ** spans components.
    Walking pairs of glob positions accounts for both patterns' wildcards.
    """
    def intersect(a, b, star: str, compatible: Callable[[str, str], bool]) -> bool:
        pending, seen = [(0, 0)], set()
        while pending:
            i, j = pending.pop()
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if i == len(a) and j == len(b):
                return True
            if i < len(a) and a[i] == star:
                pending.append((i + 1, j))
            if j < len(b) and b[j] == star:
                pending.append((i, j + 1))
            if i < len(a) and j < len(b) and compatible(a[i], b[j]):
                pending.append((i + (a[i] != star), j + (b[j] != star)))
        return False

    def tokens(pattern: str):
        i = 0
        while i < len(pattern):
            start, i = i, i + 1
            if pattern[start] == "[":
                end = i
                if end < len(pattern) and pattern[end] == "!":
                    end += 1
                if end < len(pattern) and pattern[end] == "]":
                    end += 1
                while end < len(pattern) and pattern[end] != "]":
                    end += 1
                if end < len(pattern):
                    i = end + 1
            yield pattern[start:i]

    def components_overlap(a: str, b: str) -> bool:
        # Full case folding covers literal filesystem aliases such as ß/ss.
        # Leave globs unchanged so ? and character classes keep their width.
        if (
            not case_sensitive and not (a + b).isascii()
            and not has_magic(a) and not has_magic(b) and a.casefold() == b.casefold()
        ):
            return True
        # Literal/range endpoints and their successors represent every distinct
        # character class membership when matching case-sensitively.
        alphabet = {"\x01"} | {
            chr(code) for char in a + b for code in (ord(char), ord(char) + 1)
            if 0 < code <= 0x10ffff and code != ord("/")
        }
        if not case_sensitive:
            alphabet.update(chr(code) for code in range(1, 128) if code != ord("/"))
        a_tokens, b_tokens = tuple(tokens(a)), tuple(tokens(b))
        # Apply case equivalence to each token, preserving its width and the
        # original character classes (including punctuation inside [A-z]).
        matchers = {
            token: re.compile(translate(token), 0 if case_sensitive else re.IGNORECASE)
            for token in set(a_tokens + b_tokens)
        }

        def compatible(x: str, y: str) -> bool:
            if any(matchers[x].fullmatch(c) and matchers[y].fullmatch(c) for c in alphabet):
                return True
            # Unicode case mappings inside classes need not be monotonic, so
            # endpoints alone cannot prove two nonliteral tokens disjoint.
            return (
                not case_sensitive and not (x + y).isascii()
                and (len(x) > 1 or x in ("*", "?"))
                and (len(y) > 1 or y in ("*", "?"))
            )

        return intersect(
            a_tokens, b_tokens, "*", compatible,
        )

    return intersect(PurePosixPath(left).parts, PurePosixPath(right).parts, "**", components_overlap)


def _lane_has_room(running: int, waiting: int) -> bool:
    """A lane runs one issue, or a second clone while two or more issues wait."""
    return running == 0 or (running == 1 and waiting >= 2)


def _can_start_in_lane(ctx: TickContext, lane: str, issue: Issue, waiting: int) -> bool:
    running = [run for run in ctx.store.active_runs() if run.lane == lane]
    if not running:
        return True
    if not _lane_has_room(len(running), waiting):
        return False
    approval = ctx.store.load_approval(issue.number)
    run = running[0]
    running_approval = ctx.store.load_approval(run.number)
    if approval is None or running_approval is None:
        return False
    # The approval file can be replaced while the runner retains its old body.
    # Legacy records can use a snapshot approved no later than the run start.
    if run.approval_body_sha is None:
        if (
            datetime.fromisoformat(running_approval.approved_at)
            > datetime.fromisoformat(run.started_at)
        ):
            return False
    elif run.approval_body_sha != running_approval.body_sha:
        return False
    files = parse_body(approval.body).files
    running_files = parse_body(running_approval.body).files
    if not files or not running_files:
        return False
    case_sensitive = _filesystem_case_sensitive(ctx.repo_root)
    return case_sensitive is not None and not any(
        _file_patterns_overlap(candidate, active, case_sensitive=case_sensitive)
        for candidate in files for active in running_files
    )


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
            continue
        issue = _recover_orphan(ctx, issue, report)
        issue = _escalate_overdue_blocker(ctx, issue, report)
        if issue.state != "open":
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
    # One sensor sample per tick sizes both the test slots and the run budget.
    sample = machine_load()
    update_test_slots(
        ctx.config.test_slots, ctx.now(), pressure=sample[0], load_per_cpu=sample[1],
        swapins=getattr(sample, "swapins", None), floor=ctx.config.test_slots_floor,
    )
    prebuild_acceptance_baseline(ctx, base_ref, ctx.baseline_launcher)
    if not ctx.start_runs or not ready:
        return report
    if not in_run_hours(ctx.now().astimezone(), ctx.config.run_hours):
        return report

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


def _lane_held(ctx: TickContext, lane: str, now: datetime) -> bool:
    """A lane waits while it, its implementer or its reviewer is paused for a usage limit."""
    if ctx.store.lane_paused(lane, now):
        return True
    settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
    config = replace(ctx.config, executor=settings.get("executor") or ctx.config.executor)
    return any(ctx.store.executor_paused(name, now) for name in {config.executor, resolved_reviewer(config)})


def _machine_budget(
    ctx: TickContext, ready: dict[str, list[int]], sample: tuple[str, float] | None = None,
) -> int:
    """Update the machine-wide AIMD value once per tick and return this tick's machine budget."""
    now = ctx.now()
    lane_runs = Counter(run.lane for run in ctx.store.active_runs())
    # A crowded lane may add a clone; the file-overlap check runs at start time.
    waiting = sum(
        1 for lane, numbers in ready.items()
        if _lane_has_room(lane_runs[lane], len(numbers)) and not _lane_held(ctx, lane, now)
    )
    sample = sample if sample is not None else machine_load()
    pressure, load = sample
    with machine_lock(ctx.store):
        state = read_state(ctx.store)
        current = int(state["parallel"]) if state else INITIAL_PARALLEL
        changed_at = datetime.fromisoformat(state["changed_at"]) if state else now
        demand = machine_runs(ctx.store, now) + waiting
        # Work only caps growth: the shared value never drops below itself for lack
        # of work here, so it stays the pressure- and load-driven value.
        wanted = next_parallel(
            current, pressure=pressure, load_per_cpu=load, lanes_with_work=max(demand, current),
            ceiling=None, increase_ok=now - changed_at >= INCREASE_INTERVAL,
        )
        if wanted < current and not decrease_allowed(state, now):
            wanted = current
        value = wanted if pressure == "critical" else max(ctx.config.parallel_floor, wanted)
        if state is None or value != current:
            decreased_at = now.astimezone(UTC).isoformat() if value < current else (state or {}).get("decreased_at")
            write_state(ctx.store, value, now, decreased_at=decreased_at)
        append_budget_history(
            ctx.store.root.parent, now, "parallel", current, value,
            pressure=pressure, load_per_cpu=load, swapins=getattr(sample, "swapins", None), wanted=wanted,
        )
    return max(1, min(value, demand))


def _has_slot(ctx: TickContext, budget: int) -> bool:
    """Machine-wide runs stay under the shared budget; this project's under its ceiling."""
    if machine_runs(ctx.store, ctx.now()) >= budget:
        return False
    return ctx.config.max_parallel is None or len(ctx.store.active_runs()) < ctx.config.max_parallel


def _reserve_slot(ctx: TickContext, number: int, budget: int) -> bool:
    """Recount and journal the start under the machine lock; GitHub work happens outside."""
    with machine_lock(ctx.store):
        if not _has_slot(ctx, budget):
            return False
        ctx.store.prepare_run_start(number)
        return True


def _start_ready(
    ctx: TickContext, ready: dict[str, list[int]], lane_for: Callable[[Issue], str], base_ref: str,
    report: TickReport, sample: tuple[str, float] | None = None,
) -> bool | None:
    """Start ready lanes within the machine budget; None means a GitHub wait began."""
    def eligible(issue: Issue, lane: str) -> bool:
        return (
            issue.state == "open" and not issue.is_pull_request
            and {APPROVED, READY}.issubset(issue.labels)
            and not issue.labels & {BLOCKED, NEEDS_HUMAN, RUNNING} and lane_for(issue) == lane
        )

    budget = _machine_budget(ctx, ready, sample)
    started = False
    for lane in sorted(ready, key=lane_order):
        if not _has_slot(ctx, budget):
            break
        if _lane_held(ctx, lane, ctx.now()):
            continue
        for number in ready[lane]:
            local = _local_issue(ctx, number)
            if local is not None and not eligible(local, lane):
                continue
            issue = ctx.tracker.get_issue(number)
            if not eligible(issue, lane):
                continue
            if not _can_start_in_lane(ctx, lane, issue, len(ready[lane])):
                continue
            approval = ctx.store.load_approval(number)
            event, _ = _latest_approval(ctx.tracker.label_events(number), ctx.config.approvers)
            if approval is None or event is None or event.created_at != approval.approved_at:
                continue
            if not _check_integrity(ctx, issue, approval, base_ref, report):
                continue
            if not _check_prerequisites(ctx, issue, approval, report):
                continue

            if not eligible(_local_issue(ctx, number) or issue, lane):
                continue
            if not _reserve_slot(ctx, number, budget):
                return started
            try:
                ctx.tracker.remove_labels(number, [READY])
                ctx.tracker.add_labels(number, [RUNNING])
                board.sync_status(ctx, number, "Running")
                if until := ctx.store.github_wait_until(ctx.now()):
                    report.skipped = f"GitHub rate limit until {until.isoformat()}"
                    return None
            except TrackerError as exc:
                if not exc.is_rate_limit:
                    raise
                until = orch_github.pause(ctx, exc)
                # A label may already say running even though no process started.
                # Record availability, so recovery does not charge a crash.
                ctx.store.start_run(
                    number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number),
                    lane=lane, tier=issue_size(issue.labels),
                )
                ctx.store.clear_run_start(number)
                orch_github.wait_run(ctx, number, until)
                ctx.store.finish_run(number, "limit-wait", detail=str(exc))
                report.skipped = f"GitHub rate limit until {until.isoformat()}"
                return None
            ctx.store.start_run(
                number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), lane=lane,
                tier=issue_size(issue.labels),
            )
            ctx.store.clear_run_start(number)
            pid = ctx.launcher(number)
            ctx.store.update_run(number, pid=pid)
            report.actions.append(f"#{number} started running (pid={pid})")
            started = True
            break
    return started

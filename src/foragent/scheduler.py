"""Promote approved issues and launch at most one runner per project."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import subprocess

from foragent.config import Config, in_run_hours
from foragent.git import GitRepo
from foragent.issue import acceptance_paths, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED, NEEDS_HUMAN, READY, RUNNING
from foragent.reasons import Reason, format_approval_comment, format_comment
from foragent.state import Approval, StateStore
from foragent.tracker import Issue, LabelEvent, Tracker


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


@dataclass
class TickReport:
    skipped: str | None = None
    actions: list[str] = field(default_factory=list)


def flag(
    ctx: TickContext, n: int, reason: Reason, summary: str, details: str | None = None,
) -> None:
    ctx.store.save_flag(n, reason, flagged_at=ctx.now().astimezone(UTC).isoformat())
    ctx.tracker.add_labels(n, [NEEDS_HUMAN])
    ctx.tracker.remove_labels(n, [READY, RUNNING])
    ctx.tracker.comment(n, format_comment(reason, summary, details))


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
    flag(ctx, issue.number, Reason.HASH_MISMATCH, summary, details)
    ctx.tracker.remove_labels(issue.number, [APPROVED])
    ctx.store.clear_approval(issue.number)
    report.actions.append(f"#{issue.number} flagged hash-mismatch: {summary}")
    if RUNNING in issue.labels:
        report.actions.append(f"#{issue.number} runner was already running; its process remains running in P2")


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
    return approval


def _crash_detail(ctx: TickContext, number: int) -> str:
    # The label generation makes retries after tracker failures idempotent.
    generation = sum(
        event.label == RUNNING and event.action == "labeled"
        for event in ctx.tracker.label_events(number)
    )
    return f"Crash recovery for running label generation {generation}."


def _restore_after_crash(ctx: TickContext, issue: Issue, report: TickReport) -> None:
    number = issue.number
    if issue.state != "open" or NEEDS_HUMAN in issue.labels:
        ctx.tracker.remove_labels(number, [RUNNING])
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
    else:
        ctx.tracker.add_labels(number, [READY])
        ctx.tracker.remove_labels(number, [RUNNING])
        report.actions.append(f"#{number} returned to ready after runner crash")


def _recover_orphan(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    run = ctx.store.get_run(issue.number)
    if RUNNING not in issue.labels or (run is not None and run.status == "running"):
        return issue
    detail = _crash_detail(ctx, issue.number)
    if run is None or run.outcome != "crashed" or run.detail != detail:
        ctx.store.start_run(
            issue.number, branch=f"foragent/issue-{issue.number}",
            worktree=ctx.store.worktree_path(issue.number),
        )
        ctx.store.finish_run(issue.number, outcome="crashed", detail=detail)
    report.actions.append(f"#{issue.number} orphaned running label recovered as crashed")
    _restore_after_crash(ctx, issue, report)
    return ctx.tracker.get_issue(issue.number)


def _recover_runs(ctx: TickContext, report: TickReport) -> None:
    for run in ctx.store.active_runs():
        if run.pid is None:
            dead = (
                ctx.now() - datetime.fromisoformat(run.started_at) >= timedelta(minutes=2)
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
        if not dead:
            continue
        finished = ctx.store.finish_run(run.number, outcome="crashed", detail=_crash_detail(ctx, run.number))
        if finished.outcome != "crashed":
            continue
        report.actions.append(f"#{run.number} runner crashed")
        issue = ctx.tracker.get_issue(run.number)
        _restore_after_crash(ctx, issue, report)


def _check_prerequisites(
    ctx: TickContext, issue: Issue, approval: Approval, report: TickReport,
) -> bool:
    if not has_machine_check(parse_body(approval.body)):
        flag(
            ctx, issue.number, Reason.NO_MACHINE_CHECK,
            "Cannot promote: Done when has no machine check. Add an executable code block before resuming.",
        )
        report.actions.append(f"#{issue.number} flagged no-machine-check")
        return False
    dependencies = ctx.tracker.blocked_by(issue.number)
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
        return False
    return True


def tick(ctx: TickContext) -> TickReport:
    with ctx.store.tick_lock() as acquired:
        if not acquired:
            return TickReport(skipped="another tick is running")
        return _tick_locked(ctx)


def _tick_locked(ctx: TickContext) -> TickReport:
    report = TickReport()
    ctx.git.fetch("origin", ctx.config.base_branch)
    base_ref = f"origin/{ctx.config.base_branch}"
    _recover_runs(ctx, report)
    ready: list[int] = []

    for issue in sorted(ctx.tracker.list_open_issues(), key=lambda issue: issue.number):
        issue = _recover_orphan(ctx, issue, report)
        previous_flag = ctx.store.load_flag(issue.number) if NEEDS_HUMAN in issue.labels else None
        if NEEDS_HUMAN in issue.labels and (
            previous_flag is None or previous_flag.reason != Reason.HASH_MISMATCH
        ):
            continue
        if APPROVED not in issue.labels:
            continue
        event, ignored = _latest_approval(ctx.tracker.label_events(issue.number), ctx.config.approvers)
        if ignored:
            report.actions.append(
                f"#{issue.number} ignored approved label from non-approver(s): {', '.join(sorted(ignored))}"
            )
        if event is None:
            continue
        if NEEDS_HUMAN in issue.labels:
            if datetime.fromisoformat(event.created_at) <= datetime.fromisoformat(previous_flag.flagged_at):
                continue
            ctx.tracker.remove_labels(issue.number, [NEEDS_HUMAN])
            issue = replace(issue, labels=issue.labels - {NEEDS_HUMAN})
            report.actions.append(f"#{issue.number} resumed after re-approval")

        approval = ctx.store.load_approval(issue.number)
        if approval is None or approval.approved_at != event.created_at:
            issue = ctx.tracker.get_issue(issue.number)
            if issue.state != "open" or issue.is_pull_request or APPROVED not in issue.labels:
                continue
            approval = _snapshot(ctx, issue, event, base_ref, report)
            if approval is None:
                continue
        elif not _check_integrity(ctx, issue, approval, base_ref, report):
            continue
        if RUNNING in issue.labels:
            continue
        if not _check_prerequisites(ctx, issue, approval, report):
            continue
        if READY in issue.labels:
            ready.append(issue.number)
            continue

        ctx.tracker.add_labels(issue.number, [READY])
        report.actions.append(f"#{issue.number} promoted to ready")
        ready.append(issue.number)

    if ctx.store.active_runs() or not ctx.start_runs or not ready:
        return report
    if not in_run_hours(ctx.now().astimezone(), ctx.config.run_hours):
        return report

    number = min(ready)
    issue = ctx.tracker.get_issue(number)
    if (
        issue.state != "open" or issue.is_pull_request
        or not {APPROVED, READY}.issubset(issue.labels)
        or issue.labels & {NEEDS_HUMAN, RUNNING}
    ):
        return report
    approval = ctx.store.load_approval(number)
    event, _ = _latest_approval(ctx.tracker.label_events(number), ctx.config.approvers)
    if approval is None or event is None or event.created_at != approval.approved_at:
        return report
    if not _check_integrity(ctx, issue, approval, base_ref, report):
        return report
    if not _check_prerequisites(ctx, issue, approval, report):
        return report

    ctx.tracker.remove_labels(number, [READY])
    ctx.tracker.add_labels(number, [RUNNING])
    ctx.store.start_run(number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number))
    pid = ctx.launcher(number)
    ctx.store.update_run(number, pid=pid)
    report.actions.append(f"#{number} started running (pid={pid})")
    return report

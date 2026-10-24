"""Per-issue prerequisite reads, blocker escalation and batch reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING

from foragent import board, scheduler
from foragent.git import GitError
from foragent.labels import BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.orch_events import append_event
from foragent.reasons import Reason, format_comment
from foragent.state import Approval
from foragent.tracker import Comment, Dependency, Issue, LabelEvent, PullRequest, TrackerError, local_issue_models

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

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
    events = scheduler._confirmed_read(
        ctx, number, "label_events", lambda: [asdict(event) for event in ctx.tracker.label_events(number)],
        confirmed=confirmed,
    )
    return [LabelEvent(**event) for event in events]


def _open_pr(ctx: TickContext, number: int, *, confirmed: bool) -> int | None:
    branch = f"foragent/issue-{number}"
    return scheduler._confirmed_read(ctx, number, "open_pr", lambda: ctx.tracker.find_pr(branch), confirmed=confirmed)


def _blockers(ctx: TickContext, number: int, *, confirmed: bool) -> list[Dependency]:
    # Each blocker is a fact link: a change to any blocker voids the remembered list.
    dependencies = scheduler._confirmed_read(
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
        scheduler._open_pr(ctx, issue.number, confirmed=confirmed) is not None
        and (latest_run is None or latest_run.outcome != "limit-wait")
    ):
        if READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [READY])
        board.sync_status(ctx, issue, "PR open")
        return False
    if not scheduler.has_machine_check(scheduler.parse_body(approval.body)):
        scheduler.flag(
            ctx, issue.number, Reason.NO_MACHINE_CHECK,
            "Cannot promote: Done when has no machine check. Add an executable code block before resuming.",
        )
        report.actions.append(f"#{issue.number} flagged no-machine-check")
        return False
    dependencies = scheduler._blockers(ctx, issue.number, confirmed=confirmed)
    cancelled = next(
        (dependency for dependency in dependencies
         if dependency.state == "closed" and dependency.state_reason == "not_planned"), None,
    )
    if cancelled is not None:
        scheduler.flag(
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


def _escalate_overdue_blocker(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    issue = scheduler._local_issue(ctx, issue.number) or issue
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
            scheduler._batch_pr(parent, ctx)
        except (GitError, TrackerError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            report.actions.append(f"#{parent} batch PR finalization deferred: {exc}")

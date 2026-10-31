"""Per-issue prerequisite reads, blocker escalation, closed-issue catch-up and batch reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from foragent import orch_park, scheduler
from foragent.records import load_record
from foragent.state import Approval
from foragent.tracker import Dependency, Issue, LabelEvent

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
        ctx, number, "label_events", lambda: [scheduler.asdict(event) for event in ctx.tracker.label_events(number)],
        confirmed=confirmed,
    )
    return [load_record(scheduler.LabelEvent, event) for event in events]


def _open_pr(ctx: TickContext, number: int, *, confirmed: bool) -> int | None:
    branch = f"foragent/issue-{number}"
    return scheduler._confirmed_read(ctx, number, "open_pr", lambda: ctx.tracker.find_pr(branch), confirmed=confirmed)


def _blockers(ctx: TickContext, number: int, *, confirmed: bool) -> list[Dependency]:
    # Each blocker is a fact link: a change to any blocker voids the remembered list.
    dependencies = scheduler._confirmed_read(
        ctx, number, "blocked_by", lambda: [scheduler.asdict(dependency) for dependency in ctx.tracker.blocked_by(number)],
        confirmed=confirmed, links=lambda value: [
            dependency["number"] if dependency.get("repository") is None
            else f"{dependency['repository']}#{dependency['number']}" for dependency in value
        ],
    )
    return [load_record(scheduler.Dependency, dependency) for dependency in dependencies]


def _check_prerequisites(
    ctx: TickContext, issue: Issue, approval: Approval, report: TickReport, *, confirmed: bool = False,
) -> bool:
    latest_run = next(
        (run for run in reversed(ctx.store.read_history()) if run.number == issue.number), None,
    )
    if (
        latest_run is not None and latest_run.outcome == "merged"
        and scheduler.datetime.fromisoformat(approval.approved_at) <= scheduler.datetime.fromisoformat(latest_run.started_at)
    ):
        if scheduler.READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [scheduler.READY])
        scheduler.board.sync_status(ctx, issue, "Done")
        return False
    if (
        scheduler._open_pr(ctx, issue.number, confirmed=confirmed) is not None
        and (latest_run is None or latest_run.outcome != "limit-wait")
    ):
        if scheduler.READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [scheduler.READY])
        scheduler.board.sync_status(ctx, issue, "PR open")
        return False
    if not scheduler.has_machine_check(scheduler.parse_body(approval.body)):
        scheduler.flag(
            ctx, issue.number, scheduler.Reason.NO_MACHINE_CHECK,
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
            ctx, issue.number, scheduler.Reason.SPEC_QUESTION,
            f"Cannot promote: blocker #{cancelled.number} was closed as not planned. Clarify the dependency.",
        )
        report.actions.append(f"#{issue.number} flagged spec-question: blocker #{cancelled.number} was closed as not planned")
        return False
    if any(dependency.state == "open" for dependency in dependencies):
        if scheduler.READY in issue.labels:
            ctx.tracker.remove_labels(issue.number, [scheduler.READY])
            report.actions.append(f"#{issue.number} returned to approved waiting for open blockers")
        scheduler.board.sync_status(ctx, issue, "Approved")
        return False
    return True


def _escalate_overdue_blocker(ctx: TickContext, issue: Issue, report: TickReport) -> Issue:
    issue = scheduler._local_issue(ctx, issue.number) or issue
    if issue.state != "open" or scheduler.BLOCKED not in issue.labels or scheduler.NEEDS_HUMAN in issue.labels:
        return issue
    row = next((row for row in ctx.store.local_issues() if row["number"] == issue.number), {})
    comments = [
        comment for comment in (load_record(scheduler.Comment, item) for item in row.get("comments", []))
        if scheduler.re.fullmatch(
            r"<!-- foragent:blocked reason=[a-z-]+ -->",
            comment.body.partition("\n")[0].removesuffix("\r"),
        )
    ]
    latest = max(comments, key=lambda c: (scheduler.datetime.fromisoformat(c.created_at), c.id), default=None)
    if latest is None:
        return issue
    # The runner's blocker comment can reach the local record after its run ends,
    # so the clock starts at the later of the newest blocker and the newest run end.
    since = max([scheduler.datetime.fromisoformat(latest.created_at)] + [
        scheduler.datetime.fromisoformat(ended)
        for (number, _), ended in ctx.store.finish_times().items() if number == issue.number
    ])
    if ctx.now() - since <= scheduler.timedelta(minutes=ctx.config.orchestrator_timeout_minutes):
        return issue
    # A park newer than the clock's start is the orchestrator's response to these blockers.
    parked = orch_park.parked_at(ctx.store, issue.number, comments=row.get("comments", []))
    if parked is not None and parked > since:
        return issue
    ctx.tracker.add_labels(issue.number, [scheduler.NEEDS_HUMAN])
    scheduler.board.sync_status(ctx, issue, "Needs human")
    ctx.tracker.comment(issue.number, scheduler.format_comment(
        scheduler.Reason.ORCHESTRATOR_TIMEOUT,
        "The orchestrator has not responded to this blocker.",
        f"Blocked since {latest.created_at}; timeout: {ctx.config.orchestrator_timeout_minutes} minutes.\n\n"
        f"Latest blocker:\n{latest.body}",
    ))
    report.actions.append(f"#{issue.number} flagged orchestrator-timeout")
    scheduler.append_event(ctx.store, ctx.now(), issue.number, "orchestrator-timeout",
                 "Orchestrator did not respond to the blocker in time; escalated to needs-human.")
    return scheduler.replace(issue, labels=issue.labels | {scheduler.NEEDS_HUMAN})


def _reconcile_batches(ctx: TickContext, report: TickReport) -> None:
    # Housekeeping first, on every tick: issues closed outside the gate since the
    # last tick (one light read) reach this reconciliation and the board sync.
    if (observe := getattr(ctx.tracker, "observe_closed_issues", None)) is not None:
        observe(observed_at=ctx.now())
    issues = {issue.number: issue for issue in scheduler.local_issue_models(ctx.store) if issue.state == "open"}
    parents: set[int] = set()
    rows = ctx.store.local_issues()
    prs = [scheduler.PullRequest(row["pr_number"], row["pr_head"], row["pr_base"], row["title"], row["body"])
           for row in rows if row["merged"] and row.get("pr_head") and row.get("pr_base")]
    # The gate closes an order parent once its children are done (#411), possibly
    # before its batch PR exists; finalization continues until that PR merged.
    batch_merged = {row["number"] for row in rows
                    if row["merged"] and row.get("pr_head") == f"foragent/batch-{row['number']}"}
    closed_parents = {row["number"] for row in rows if row["state"] == "closed"} - batch_merged
    for pr in prs:
        child_match = scheduler.re.fullmatch(r"foragent/issue-(\d+)", pr.head)
        batch_match = scheduler.re.fullmatch(r"foragent/batch-(\d+)", pr.base)
        if child_match is None or batch_match is None:
            continue
        number, parent = int(child_match[1]), int(batch_match[1])
        if parent in issues or parent in closed_parents:
            parents.add(parent)
        if number not in issues or ctx.tracker.parent_of(number) != parent:
            continue
        ctx.tracker.close_issue(number, f"Merged into {pr.base} (PR #{pr.number})")
        ctx.tracker.remove_labels(number, [scheduler.READY, scheduler.RUNNING])
        scheduler.board.sync_status(ctx, number, "Done")
        del issues[number]
        report.actions.append(f"#{number} closed after batch PR #{pr.number} merged")
    for parent in sorted(parents):
        branch = f"foragent/batch-{parent}"
        if ctx.tracker.find_pr(branch) is not None:
            continue
        try:
            scheduler._batch_pr(parent, ctx)
        except (scheduler.GitError, scheduler.TrackerError) as exc:
            if isinstance(exc, scheduler.TrackerError) and exc.is_rate_limit:
                raise
            report.actions.append(f"#{parent} batch PR finalization deferred: {exc}")

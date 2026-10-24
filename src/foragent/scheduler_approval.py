"""Approval snapshots and their integrity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent import scheduler
from foragent.state import Approval
from foragent.tracker import Issue, LabelEvent

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _latest_approval(events: list[LabelEvent], approvers: list[str]) -> tuple[LabelEvent | None, set[str]]:
    approval = None
    ignored: set[str] = set()
    allowed = {actor.casefold() for actor in approvers}
    for event in sorted(events, key=lambda event: scheduler.datetime.fromisoformat(event.created_at)):
        if event.label != scheduler.APPROVED:
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
        scheduler._kill_run(ctx, run.number, report, detail=summary)
    scheduler.invalidate_approval(ctx, issue.number, summary, details)
    report.actions.append(f"#{issue.number} flagged hash-mismatch: {summary}")


def _check_integrity(
    ctx: TickContext, issue: Issue, approval: Approval, base_ref: str, report: TickReport,
) -> bool:
    changed_body = scheduler.body_hash(issue.body) != approval.body_sha
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
    scheduler._invalidate_approval(ctx, issue, summary, "\n".join(details), report)
    return False


def _snapshot(
    ctx: TickContext, issue: Issue, event: LabelEvent, base_ref: str, report: TickReport,
) -> Approval | None:
    edited_at = ctx.tracker.body_last_edited_at(issue.number)
    if edited_at is not None and scheduler.datetime.fromisoformat(edited_at) >= scheduler.datetime.fromisoformat(event.created_at):
        scheduler._invalidate_approval(
            ctx, issue,
            "Approval invalidated: approval predates an edit to the issue body. Re-approve to run it.",
            f"Last body edit: {edited_at}; approval: {event.created_at}.", report,
        )
        return None

    parsed = scheduler.parse_body(issue.body)
    paths = scheduler.acceptance_paths(parsed.done_when_commands, ctx.config.acceptance_dir)
    # 알려진 한계: 커밋 시각 기준이라 승인 전에 만들어 승인 후 push한 커밋은 구분 못 함
    approved_ref = ctx.git.commit_before(base_ref, event.created_at) if paths else None
    if paths and approved_ref is None:
        scheduler._invalidate_approval(
            ctx, issue,
            "Approval invalidated: no base commit exists at the approval time. Re-approve to run it.",
            f"Cannot verify acceptance files on {base_ref} at {event.created_at}.", report,
        )
        return None
    blobs = ctx.git.blob_shas(approved_ref, paths) if approved_ref is not None else {}
    approval = scheduler.Approval(issue.number, issue.body, scheduler.body_hash(issue.body), blobs, event.actor, event.created_at)
    if not scheduler._check_integrity(ctx, issue, approval, base_ref, report):
        return None
    ctx.store.save_approval(
        issue.number, body=approval.body, body_sha=approval.body_sha, acceptance_blobs=blobs,
        approved_by=event.actor, approved_at=event.created_at,
    )
    ctx.tracker.comment(
        issue.number, scheduler.format_approval_comment(approval.body_sha, blobs, event.actor, event.created_at),
    )
    report.actions.append(f"#{issue.number} approval snapshot saved")
    scheduler.board.sync_status(ctx, issue, "Approved")
    return approval

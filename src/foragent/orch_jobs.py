"""The gate's worker for durable preread/approval jobs a PM session submitted (#1488).

``order_jobs`` (#1487) owns the queue: a session only submits a content snapshot and the
person's explicit authorization. Each tick, this worker runs the unfinished jobs itself,
so the work does not depend on the PM session that asked for it:

- ``started`` -> the preread runs through the established ``order.preread``.
- ``prereaded`` keeps the result, so a later tick never asks the agent again.
- A job with an unknown ends ``question``: each unknown waits in the inbox for the next
  PM, and nothing is approved.
- A clean job without authorization ends ``clean``. With ``approve_if_clean`` the
  established ``order.publish(approve=True)`` publishes and approves, the reader's
  actual answer is commented on each child, and the job ends ``approved``.

The worker approves only the content that was submitted: when the files the plan
publishes no longer match the snapshot, the job ends ``failed`` instead.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from foragent import order, order_jobs, orch_inbox
from foragent.orch_events import append_event
from foragent.git import GitError
from foragent.tracker import RecordingTracker, TrackerError

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

MAX_ATTEMPTS = 3
MARKER = "<!-- foragent:order-job job={job} key={key} -->"
# Events after which this worker has nothing left to do for the job.
_IDLE = order_jobs.FINISHED | {"queued"}


def tick(ctx: TickContext, report: TickReport) -> None:
    """Run every unfinished job once, oldest first; each step is recorded before the next."""
    for job in order_jobs.jobs(ctx.store):
        if job["state"] in _IDLE:
            continue
        try:
            _run(ctx, report, job)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise  # The tick pauses for GitHub; the job resumes from its last recorded step.
            line = f"Order job {job['job'][:12]} ({job['title']}): deferred: {' '.join(str(exc).split())}"
            report.actions.append(line)
            append_event(ctx.store, ctx.now(), None, "order-job-deferred", line)


def _finish(ctx, report, job: dict, event: str, summary: str, **facts) -> None:
    order_jobs.record(ctx.store, job["job"], event, **facts)
    line = f"Order job {job['job'][:12]} ({job['title']}): {summary}"
    report.actions.append(line)
    append_event(ctx.store, ctx.now(), None, f"order-job-{event}", line)


def _run(ctx: TickContext, report: TickReport, job: dict) -> None:
    store, root, config = ctx.store, ctx.repo_root, ctx.config
    try:
        plan, files = order_jobs.snapshot(store, job["job"])
        current = order.validate_plan(plan, root, config)
    except (KeyError, ValueError, OSError) as exc:  # PlanError is a ValueError.
        _finish(ctx, report, job, "failed", f"cannot read the submitted content: {exc}", reason=str(exc))
        return
    if current != files:
        # Authorization covers the submitted bytes only; changed files need a new submission.
        _finish(ctx, report, job, "failed", "the files changed after submission", reason="content-changed")
        return
    # The job's GitHub writes are outside writes for this gate: its next listing reads them,
    # as it reads a PM's own publication, so this tick's admission never sees half of them.
    tracker = ctx.tracker._tracker if isinstance(ctx.tracker, RecordingTracker) else ctx.tracker
    events = [row["event"] for row in job["events"]]
    if "prereaded" in events:
        results = next(row for row in reversed(job["events"]) if row["event"] == "prereaded")["results"]
    else:
        if events.count("started") >= MAX_ATTEMPTS:
            _finish(ctx, report, job, "failed", f"preread did not finish in {MAX_ATTEMPTS} attempts",
                    reason="attempts")
            return
        order_jobs.record(store, job["job"], "started")
        try:
            results = order.preread(plan, root, config, lambda: tracker, repo=ctx.repo, git=ctx.git)
        except order.PlanError as exc:
            _finish(ctx, report, job, "failed", f"preread refused: {exc}", reason=str(exc))
            return
        results = [{"key": row["key"], "unknowns": list(row["unknowns"])} for row in results]
        order_jobs.record(store, job["job"], "prereaded", results=results)
    open_items = [row for row in results if row["unknowns"]]
    if open_items:
        for row in open_items:
            for index, unknown in enumerate(row["unknowns"]):
                orch_inbox.ask_once(store, f"order-job:{job['job']}:{row['key']}:{index}",
                                    f"{job['title']} / {row['key']}: {unknown}", [], None)
        _finish(ctx, report, job, "question", "the implementer asked; waiting for the PM",
                keys=[row["key"] for row in open_items])
        return
    if not job["approve_if_clean"]:
        _finish(ctx, report, job, "clean", "preread is clean")
        return
    try:  # The established approval path: it approves only a clean preread of the current content.
        published = order.publish(plan, root, config, tracker, repo=ctx.repo, git=ctx.git, approve=True)
    except (order.PlanError, GitError) as exc:
        _finish(ctx, report, job, "failed", f"publication refused: {exc}", reason=str(exc))
        return
    if published.publication_request is not None:
        _finish(ctx, report, job, "queued", "publication queued", request=published.publication_request)
        return
    numbers = _numbers(plan, published.rows)
    _record_evidence(tracker, plan, root, config, job, numbers)
    _finish(ctx, report, job, "approved", "preread clean; published and approved",
            issues=sorted(numbers.values()))


def _numbers(plan: dict, rows: list[dict]) -> dict[str, int]:
    items = [*plan["issues"], *plan.get("edits", [])]
    return {item.get("key", f"#{item.get('number')}"): row["number"] for item, row in zip(items, rows)}


def _record_evidence(tracker, plan: dict, root, config, job: dict, numbers: dict[str, int]) -> None:
    """Comment each reader's actual answer on its issue once; a retry skips what is there."""
    records = order._preread_records(root)
    items = [item for item in plan["issues"] if "statements" not in item]
    for item in items:
        number = numbers.get(item["key"])
        record = records.get(order.preread_target(plan, item, config, root)[2])
        if number is None or not isinstance(record, dict):
            continue
        marker = MARKER.format(job=job["job"], key=item["key"])
        if any(comment.body.startswith(marker) for comment in tracker.list_comments(number)):
            continue
        answer = record.get("answer")
        answer = answer.strip() if isinstance(answer, str) else ""
        fence = "`" * max(3, *(len(run) + 1 for run in re.findall(r"`+", answer)), 3)
        tracker.comment(number, f"{marker}\nPreread by the lane implementer (background job "
                                f"{job['job'][:12]}), recorded by FA:\n\n{fence}text\n"
                                f"{answer or '(no answer was recorded)'}\n{fence}")

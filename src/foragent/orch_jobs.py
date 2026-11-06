"""The gate's worker for durable preread/approval jobs a PM session submitted (#1488).

``order_jobs`` (#1487) owns the queue: a session only submits a content snapshot and the
person's explicit authorization. Each tick, this worker runs the unfinished jobs itself,
so the work does not depend on the PM session that asked for it:

- ``started`` -> the preread runs through the established ``order.preread``.
- ``prereaded`` keeps each reader's result and actual answer, so a later tick never asks
  the agent again and every later step reads the answer from the job log.
- A job with an unknown ends ``question``: each unknown waits in the inbox for the next
  PM, and nothing is approved.
- A clean job without authorization ends ``clean``. With ``approve_if_clean`` the
  established ``order.publish(approve=True)`` publishes and approves, and the job ends
  ``approved``. With ``merge_owner`` that call only queues a publication request: the job
  is ``queued`` and ends ``approved`` once the owner marks the request ``published``, or
  ``failed`` when the owner returns it to preparation.
- In every outcome, the reader's answer is commented once on each issue of the order that
  already exists (published children and edited issues).

The worker approves only the content that was submitted: when the files the plan
publishes no longer match the snapshot, the job ends ``failed`` instead.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from foragent import order, order_jobs, order_publications, orch_inbox
from foragent.orch_events import append_event
from foragent.git import GitError
from foragent.tracker import RecordingTracker, TrackerError

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

MAX_ATTEMPTS = 3
MARKER = "<!-- foragent:order-job job={job} key={key} -->"
# Events after which this worker has nothing left to do for the job. A ``queued`` job is not
# idle: the merge owner publishes it, and the worker finishes the job from that request.
_IDLE = order_jobs.FINISHED


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
    # The job's GitHub writes are outside writes for this gate: its next listing reads them,
    # as it reads a PM's own publication, so this tick's admission never sees half of them.
    tracker = ctx.tracker._tracker if isinstance(ctx.tracker, RecordingTracker) else ctx.tracker
    if job["state"] == "queued":
        _follow_publication(ctx, report, job, tracker)
        return
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
    events = [row["event"] for row in job["events"]]
    if "prereaded" in events:
        results = _results(job)
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
        answers = _answers(root, plan["id"])
        # The reader's own words live in the job log, the one record every later step reads.
        results = [{"key": row["key"], "unknowns": list(row["unknowns"]), "answer": answers.get(row["key"], "")}
                   for row in results]
        order_jobs.record(store, job["job"], "prereaded", results=results)
    open_items = [row for row in results if row["unknowns"]]
    if open_items:
        for row in open_items:
            for index, unknown in enumerate(row["unknowns"]):
                orch_inbox.ask_once(store, f"order-job:{job['job']}:{row['key']}:{index}",
                                    f"{job['title']} / {row['key']}: {unknown}", [], None)
        _record_evidence(tracker, plan, job, results)  # Issues that already exist keep the answer too.
        _finish(ctx, report, job, "question", "the implementer asked; waiting for the PM",
                keys=[row["key"] for row in open_items])
        return
    if not job["approve_if_clean"]:
        _record_evidence(tracker, plan, job, results)
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
    _approved(ctx, report, job, tracker, plan, results)


def _follow_publication(ctx, report, job: dict, tracker) -> None:
    """Finish a job the merge owner publishes; its request alone says what happened."""
    request_id = job["events"][-1].get("request")
    row = next((row for row in order_publications.requests(ctx.store) if row["id"] == request_id), None)
    if row is None:
        _finish(ctx, report, job, "failed", f"publication request {request_id} is gone", reason="request-missing")
    elif row["status"] == "published":
        plan, _files = order_jobs.snapshot(ctx.store, job["job"])
        _approved(ctx, report, job, tracker, plan, _results(job))
    elif row["status"] not in {"pending", "applying"}:
        # The owner returned it to preparation and told the PM; this authorization is used up.
        _finish(ctx, report, job, "failed", f"publication {row['status']}: {row.get('reason')}",
                reason=row.get("reason") or row["status"])


def _approved(ctx, report, job: dict, tracker, plan: dict, results: list[dict]) -> None:
    numbers = _record_evidence(tracker, plan, job, results)
    _finish(ctx, report, job, "approved", "preread clean; published and approved",
            issues=sorted(numbers.values()))


def _results(job: dict) -> list[dict]:
    return next(row for row in reversed(job["events"]) if row["event"] == "prereaded")["results"]


def _answers(root, plan_id: str) -> dict[str, str]:
    """Each key's latest preread answer for this plan; file order is write order."""
    answers = {}
    for record in order._preread_records(root).values():
        if isinstance(record, dict) and record.get("plan") == plan_id and isinstance(record.get("answer"), str):
            answers[record["key"]] = record["answer"]
    return answers


def _issue_numbers(tracker, plan: dict) -> dict[str, int]:
    """The order's own key -> issue map: published children and every edit."""
    numbers = {f"#{item['number']}": item["number"] for item in plan.get("edits", [])}
    try:
        numbers.update(order._publication_targets(plan, tracker.list_issues())[1])
    except order.PlanError:
        pass  # No consistent children map: only the edits' own numbers are known.
    return numbers


def _record_evidence(tracker, plan: dict, job: dict, results: list[dict]) -> dict[str, int]:
    """Comment each reader's actual answer on its existing issue once; a retry skips what is there."""
    numbers = _issue_numbers(tracker, plan)
    for row in results:
        number = numbers.get(row["key"])
        if number is None:
            continue
        marker = MARKER.format(job=job["job"], key=row["key"])
        if any(comment.body.startswith(marker) for comment in tracker.list_comments(number)):
            continue
        answer = (row.get("answer") or "").strip()
        fence = "`" * max(3, *(len(run) + 1 for run in re.findall(r"`+", answer)), 3)
        tracker.comment(number, f"{marker}\nPreread by the lane implementer (background job "
                                f"{job['job'][:12]}), recorded by FA:\n\n{fence}text\n"
                                f"{answer or '(no answer was recorded)'}\n{fence}")
    return numbers

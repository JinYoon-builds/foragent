"""The gate's worker for durable preread/approval jobs a PM session submitted (#1488).

``order_jobs`` (#1487) owns the queue: a session only submits a content snapshot and the
person's explicit authorization. Each tick, this worker runs the unfinished jobs itself,
so the work does not depend on the PM session that asked for it:

- ``started`` -> the preread runs through the established ``order.preread``. Every answer
  that attempt recorded is kept, even when a later item of the same preread fails.
- ``prereaded`` keeps each reader's result and actual answer, so a later tick never asks
  the agent again and every later step reads the answer from the job log.
- A job with an unknown ends ``question``: each unknown waits in the inbox for the next
  PM, and nothing is approved. A failed preread with no unknowns is retried up to
  ``MAX_ATTEMPTS`` times.
- A clean job without authorization ends ``clean``. With ``approve_if_clean`` the
  established ``order.publish(approve=True)`` publishes and approves, and the job ends
  ``approved``. With ``merge_owner`` that call only queues a publication request: the job
  is ``queued`` and ends ``approved`` once the owner marks the request ``published``, or
  ``failed`` when the owner returns it to preparation.

Every end goes through ``_close``: the outcome is commented once on the order's issue, and
each reader's answer once on its child issue, for the issues that already exist. Edited
issues get no answer comment yet (a follow-up). The worker approves only the content that
was submitted: when the files the plan publishes no longer match the snapshot, the job
ends ``failed`` instead.
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
OUTCOME = "<!-- foragent:order-job job={job} outcome -->"
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
    store, root, config = ctx.store, ctx.repo_root, ctx.config
    try:
        plan, files = order_jobs.snapshot(store, job["job"])
    except (KeyError, ValueError, OSError) as exc:
        _finish(ctx, report, job, "failed", f"cannot read the submitted content: {exc}", reason=str(exc))
        return
    done = _Close(ctx, report, job, tracker, plan)
    if job["state"] == "queued":
        _follow_publication(ctx, job, done)
        return
    try:
        current = order.validate_plan(plan, root, config)
    except (ValueError, OSError) as exc:  # PlanError is a ValueError.
        done("failed", f"the submitted content no longer validates: {exc}", reason=str(exc))
        return
    if current != files:
        # Authorization covers the submitted bytes only; changed files need a new submission.
        done("failed", "the files changed after submission", reason="content-changed")
        return
    events = [row["event"] for row in job["events"]]
    if "prereaded" in events:
        results = _results(job)
    else:
        results = _preread(ctx, job, plan, tracker, events.count("started"), done)
        if results is None:
            return
    done.results = results
    open_items = [row for row in results if row["unknowns"]]
    if open_items:
        for row in open_items:
            for index, unknown in enumerate(row["unknowns"]):
                orch_inbox.ask_once(store, f"order-job:{job['job']}:{row['key']}:{index}",
                                    f"{job['title']} / {row['key']}: {unknown}", [], None)
        keys = [row["key"] for row in open_items]
        done("question", "the implementer asked; waiting for the PM: " + ", ".join(keys), keys=keys)
        return
    if not job["approve_if_clean"]:
        done("clean", "preread is clean; approval was not authorized")
        return
    try:  # The established approval path: it approves only a clean preread of the current content.
        published = order.publish(plan, root, config, tracker, repo=ctx.repo, git=ctx.git, approve=True)
    except (order.PlanError, GitError) as exc:
        done("failed", f"publication refused: {exc}", reason=str(exc))
        return
    if published.publication_request is not None:
        _finish(ctx, report, job, "queued", "publication queued", request=published.publication_request)
        return
    done.approved()


def _preread(ctx, job: dict, plan: dict, tracker, attempts: int, done) -> list[dict] | None:
    """One preread attempt; the results to continue with, or None when this tick ends here."""
    if attempts >= MAX_ATTEMPTS:
        done("failed", f"preread did not finish in {MAX_ATTEMPTS} attempts", reason="attempts")
        return None
    before = set(_records(ctx.repo_root, plan["id"]))
    order_jobs.record(ctx.store, job["job"], "started")
    error = None
    try:
        rows = order.preread(plan, ctx.repo_root, ctx.config, lambda: tracker, repo=ctx.repo, git=ctx.git)
    except order.PlanError as exc:
        rows, error = None, str(exc)
    # What this attempt recorded, item by item: a later item's failure keeps earlier answers.
    fresh = {}
    for record_id, record in _records(ctx.repo_root, plan["id"]).items():
        if record_id not in before:
            fresh[record["key"]] = record
    if rows is not None:
        results = [{"key": row["key"], "unknowns": list(row["unknowns"]),
                    "answer": fresh.get(row["key"], {}).get("answer", "")} for row in rows]
    else:
        results = [{"key": key, "unknowns": list(record.get("unknowns") or []),
                    "answer": record.get("answer", "")} for key, record in fresh.items()]
        if not any(row["unknowns"] for row in results):
            done.results = results
            if attempts + 1 >= MAX_ATTEMPTS:
                done("failed", f"preread refused: {error}", reason=error)
            else:  # Retried on a later tick; the answers so far are on the issues already.
                done.evidence()
                order_jobs.record(ctx.store, job["job"], "attempt-failed", reason=error)
            return None
    order_jobs.record(ctx.store, job["job"], "prereaded", results=results,
                      **({"incomplete": error} if error else {}))
    return results


def _follow_publication(ctx, job: dict, done) -> None:
    """Finish a job the merge owner publishes; its request alone says what happened."""
    request_id = job["events"][-1].get("request")
    row = next((row for row in order_publications.requests(ctx.store) if row["id"] == request_id), None)
    done.results = _results(job)
    if row is None:
        done("failed", f"publication request {request_id} is gone", reason="request-missing")
    elif row["status"] == "published":
        done.approved()
    elif row["status"] not in {"pending", "applying"}:
        # The owner returned it to preparation and told the PM; this authorization is used up.
        reason = row.get("reason") or row["status"]
        done("failed", f"the merge owner returned the publication: {reason}", reason=reason)


class _Close:
    """The one way a job ends: the outcome on the order issue, answers on child issues, then the log."""

    def __init__(self, ctx, report, job: dict, tracker, plan: dict):
        self.ctx, self.report, self.job, self.tracker, self.plan = ctx, report, job, tracker, plan
        self.results: list[dict] = []

    def __call__(self, event: str, summary: str, **facts) -> None:
        parent, _numbers = self.evidence()
        _comment(self.tracker, parent, OUTCOME.format(job=self.job["job"]),
                 f"Background preread/approval job {self.job['job'][:12]} ({self.job['title']}): {summary}")
        _finish(self.ctx, self.report, self.job, event, summary, **facts)

    def approved(self) -> None:
        _parent, numbers = _issue_numbers(self.tracker, self.plan)
        issues = sorted(numbers.values())
        self("approved", "preread clean; published and approved: " + ", ".join(f"#{n}" for n in issues),
             issues=issues)

    def evidence(self) -> tuple[int | None, dict[str, int]]:
        """Comment each reader's actual answer on its existing child or edited issue once."""
        parent, numbers = _issue_numbers(self.tracker, self.plan)
        targets = {**numbers, **_edit_targets(self.plan)}
        for row in self.results:
            if row["key"] not in targets:
                continue
            answer = (row.get("answer") or "").strip()
            fence = "`" * max(3, *(len(run) + 1 for run in re.findall(r"`+", answer)), 3)
            _comment(self.tracker, targets[row["key"]], MARKER.format(job=self.job["job"], key=row["key"]),
                     f"Preread by the lane implementer (background job {self.job['job'][:12]}), recorded by FA:"
                     f"\n\n{fence}text\n{answer or '(no answer was recorded)'}\n{fence}")
        return parent, numbers


def _comment(tracker, number: int | None, marker: str, text: str) -> None:
    """Post once: a retried step finds its own marker and writes nothing."""
    if number is None or any(c.body.startswith(marker) for c in tracker.list_comments(number)):
        return
    tracker.comment(number, f"{marker}\n{text}")


def _results(job: dict) -> list[dict]:
    return next(row for row in reversed(job["events"]) if row["event"] == "prereaded")["results"]


def _records(root, plan_id: str) -> dict[str, dict]:
    """This plan's preread records by id, in write order."""
    return {record["id"]: record for record in order._preread_records(root).values()
            if isinstance(record, dict) and record.get("plan") == plan_id and isinstance(record.get("id"), str)}


def _issue_numbers(tracker, plan: dict) -> tuple[int | None, dict[str, int]]:
    """The order's own issue and its published children, from the order's existing map."""
    try:
        parent, mapping = order._publication_targets(plan, tracker.list_issues())[:2]
    except order.PlanError:
        return None, {}  # No consistent order map: nothing can be attributed to an issue.
    return parent, dict(mapping)


def _edit_targets(plan: dict) -> dict[str, int]:
    """Existing issues the plan edits, by the key their preread answers carry."""
    return {f"#{item['number']}": item["number"] for item in plan.get("edits", [])}

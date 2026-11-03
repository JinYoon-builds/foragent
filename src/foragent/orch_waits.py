"""Deliver observed prerequisite closure with the issue's authored wait (#1181).

GitHub owns the wait and native dependencies. Only the gate, under its tick lock,
adds wait notices through orch_inbox; the request itself is the durable delivery
receipt, including after it is done. No separate claims or work ledger exist.
"""

from __future__ import annotations

import hashlib
import json

from foragent import orch_inbox, order_waiting, scheduler
from foragent.tracker import TrackerError, local_issue_models


def _deliver(ctx, issue, delivered: set[str]) -> None:
    waiting = order_waiting.section(issue.body)
    if not waiting:
        return
    blockers = scheduler._blockers(ctx, issue.number, confirmed=True)
    if not blockers or any(blocker.state != "closed" for blocker in blockers):
        return
    closed = sorted({(blocker.repository or ctx.repo, blocker.number) for blocker in blockers})
    digest = hashlib.sha256(waiting.encode("utf-8")).hexdigest()
    key = json.dumps([issue.number, digest, closed], separators=(",", ":"))
    if key in delivered:
        return
    evidence = ", ".join(f"{repo}#{number}" for repo, number in closed)
    message = (f"Native prerequisites closed for #{issue.number}: {evidence}.\n"
               f"{waiting}\n"
               "To the responsible role(s): re-read the issue and the closed prerequisites, "
               "judge the next condition, then update or clear its ## Waiting section through "
               "the order tools. This notice does not grant approval.")
    orch_inbox.add_request(ctx.store, "gate", message, {
        "wait_resume_key": key, "issue": issue.number, "waiting": waiting,
        "closed_blockers": [{"repository": repo, "number": number} for repo, number in closed],
    })
    delivered.add(key)


def _failed(ctx, report, exc: Exception) -> None:
    if isinstance(exc, TrackerError) and exc.is_rate_limit:
        raise exc
    report.actions.append(scheduler.text("tick_step_failed", ctx.config.language,
                                         step="waiting_resume", error=scheduler._one_line(exc)))


def tick(ctx, report, *, confirmed: bool) -> None:
    """Scan only after a successful current listing, which can confirm blocker reads.

    Between refreshes there is no new evidence. A failed refresh must not make an
    old wait look current. Native closure is evidence to the role, never a prose
    condition interpreter or permission to approve/start an issue.
    """
    if not confirmed:
        return
    try:
        delivered = {row["wait_resume_key"] for row in orch_inbox.requests(ctx.store)
                     if row.get("from") == "gate" and isinstance(row.get("wait_resume_key"), str)}
        issues = local_issue_models(ctx.store)
    except Exception as exc:
        _failed(ctx, report, exc)
        return
    for issue in issues:
        if issue.state == "open":
            try:
                _deliver(ctx, issue, delivered)
            except Exception as exc:
                _failed(ctx, report, exc)

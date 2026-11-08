"""The queue at an approval, recorded once by the approval step that saves the snapshot.

``scheduler_approval._snapshot`` is the one writer: right after it saves an approval it appends
one ``approval-queue`` flow record with the approval time, the issue's size label and the numbers
of the other issues that were queued (open, approved and ready, not held, no open native
blocker) or running at that moment. Readers (the scorecard's standalone latency) use only this
fact, so later label changes, cache deletions or log rotation can never rebuild a past queue: a
missing or rotated record is missing evidence.
"""

from __future__ import annotations

from typing import Any

from foragent.labels import issue_size

KIND = "approval-queue"
HELD = frozenset({"blocked", "needs-human"})


def _queued(row: dict[str, Any]) -> bool:
    labels = set(row.get("labels") or ())
    if row.get("state") != "open" or row.get("is_pull_request"):
        return False
    if "running" in labels:
        return True
    blocked = any(isinstance(edge, dict) and edge.get("state") == "open" for edge in row.get("blocked_by") or ())
    return {"approved", "ready"} <= labels and not labels & HELD and not blocked


def others(store, number: int) -> list[int]:
    """Other issues queued or running now, from the gate's local rows and run records."""
    busy = {row["number"] for row in store.local_issues() if row.get("number") != number and _queued(row)}
    busy |= {run.number for run in store.active_runs() if run.status == "running" and run.number != number}
    return sorted(busy)


def record(store, issue, approved_at: str) -> None:
    """Append the approval-time queue fact. Like ``record_flow`` it never fails the approval:
    an unreadable queue leaves no fact, which readers count as missing evidence."""
    try:
        busy = others(store, issue.number)
        sized = any(str(label).startswith("size:") for label in issue.labels)
        store.record_flow(KIND, issue=issue.number, approved_at=approved_at,
                          size=issue_size(issue.labels) if sized else None, others=busy)
    except Exception:
        return

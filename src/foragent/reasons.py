"""Machine-readable comments for decisions and approval snapshots."""

from datetime import datetime
from enum import StrEnum
import re
from typing import Literal


class Reason(StrEnum):
    SPEC_QUESTION = "spec-question"
    HASH_MISMATCH = "hash-mismatch"
    ALWAYS_HUMAN_PATH = "always-human-path"
    ACCEPTANCE_TOUCHED = "acceptance-touched"
    RISK_HIGH = "risk-high"
    RETRIES_EXCEEDED = "retries-exceeded"
    STALLED = "stalled"
    TIMEOUT = "timeout"
    STOP_REFUSED = "stop-refused"
    NO_MACHINE_CHECK = "no-machine-check"
    ORCHESTRATOR_TIMEOUT = "orchestrator-timeout"
    REVIEW_DISPUTE = "review-dispute"
    ACCEPTANCE_BASELINE_UNAVAILABLE = "acceptance-baseline-unavailable"
    CI_NOT_STARTED = "ci-not-started"
    TEST_HARNESS_CHANGED = "test-harness-changed"


GROUP: dict[Reason, str] = {
    Reason.SPEC_QUESTION: "decision",
    Reason.HASH_MISMATCH: "reapprove",
    Reason.ALWAYS_HUMAN_PATH: "review",
    Reason.ACCEPTANCE_TOUCHED: "review",
    Reason.RISK_HIGH: "review",
    Reason.RETRIES_EXCEEDED: "tech-failure",
    Reason.STALLED: "tech-failure",
    Reason.TIMEOUT: "tech-failure",
    Reason.STOP_REFUSED: "tech-failure",
    Reason.NO_MACHINE_CHECK: "decision",
    Reason.ORCHESTRATOR_TIMEOUT: "tech-failure",
    Reason.REVIEW_DISPUTE: "tech-failure",
    Reason.ACCEPTANCE_BASELINE_UNAVAILABLE: "tech-failure",
    Reason.CI_NOT_STARTED: "tech-failure",
    Reason.TEST_HARNESS_CHANGED: "review",
}

APPROVAL_MARKER = "<!-- foragent:approved"


def format_comment(
    reason: Reason, summary: str, details: str | None = None, *,
    label: Literal["blocked", "needs-human"] = "needs-human",
) -> str:
    summary = " ".join(summary.split())
    body = f"<!-- foragent:{label} reason={reason.value} -->\n**{summary}**"
    return f"{body}\n\n{details}" if details else body


def marked_reason(comment_body: str) -> str | None:
    """Read a blocker marker, including reason codes a newer version introduced."""
    first_line = comment_body.split("\n", 1)[0].removesuffix("\r")
    match = re.fullmatch(r"<!-- foragent:(?:blocked|needs-human) reason=([a-z-]+) -->", first_line)
    return match[1] if match else None


def current_blocker(row: dict, pending=()) -> tuple[dict, str] | None:
    """Current waiting labels own blockage; comments supply its reason and actual time.

    Pending comments are explicit undelivered intent, in outbox FIFO order.
    They take precedence over delivered comments until observation ingest retires them.
    """
    labels = set(row.get("labels", ())) & {"blocked", "needs-human"}
    if row.get("is_pull_request", False) or row.get("state") != "open" or not labels:
        return None

    def relevant(body):
        return marked_reason(body) is not None and any(
            body.startswith(f"<!-- foragent:{label} ") for label in labels)

    intents = [entry for entry in pending if entry["issue"] == row["number"]
               and entry["action"] == "comment" and relevant(entry["body"])]
    if intents:
        entry = intents[-1]
        comment = {"body": entry["body"], "created_at": entry["queued_at"], "id": -entry["id"]}
    else:
        comments = [comment for comment in row.get("comments", ()) if relevant(comment["body"])]
        comment = max(comments, key=lambda c: (datetime.fromisoformat(c["created_at"]), c["id"]), default=None)
    return (comment, marked_reason(comment["body"])) if comment else None


def parse_reason(comment_body: str) -> Reason | None:
    try:
        return Reason(marked_reason(comment_body))
    except ValueError:
        return None


def format_approval_comment(
    body_sha: str,
    acceptance_blobs: dict[str, str],
    approved_by: str,
    approved_at: str,
) -> str:
    lines = [
        f"{APPROVAL_MARKER} sha256={body_sha} by={approved_by} at={approved_at} -->",
        "Approved issue snapshot. Acceptance file blob hashes:",
    ]
    lines.extend(f"- `{path}`: `{sha}`" for path, sha in sorted(acceptance_blobs.items()))
    return "\n".join(lines)


def parse_approval_comment(body: str) -> dict | None:
    lines = body.replace("\r\n", "\n").split("\n")
    match = re.fullmatch(
        re.escape(APPROVAL_MARKER) + r" sha256=(\S+) by=(\S+) at=(\S+) -->", lines[0]
    )
    if match is None:
        return None
    blobs: dict[str, str] = {}
    for line in lines[1:]:
        item = re.fullmatch(r"- `(.+)`: `([^`]+)`", line)
        if item:
            blobs[item[1]] = item[2]
    return {
        "body_sha": match[1],
        "acceptance_blobs": blobs,
        "approved_by": match[2],
        "approved_at": match[3],
    }

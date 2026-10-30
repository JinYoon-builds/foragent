"""Machine-readable comments for decisions and approval snapshots."""

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


def parse_reason(comment_body: str) -> Reason | None:
    first_line = comment_body.split("\n", 1)[0].removesuffix("\r")
    match = re.fullmatch(r"<!-- foragent:(?:blocked|needs-human) reason=([a-z-]+) -->", first_line)
    if match is None:
        return None
    try:
        return Reason(match[1])
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

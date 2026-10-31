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


FLAG_MARKER = re.compile(r"<!-- foragent:(blocked|needs-human) reason=([a-z-]+) -->(?=[ \r\n]|$)")


SEPARATOR = " ↵ "


def one_line(marker: str, text: str = "") -> str:
    """A state-change comment: its marker and its whole text on one line.

    Only line breaks change: each becomes " ↵ ", so `flag_text` gives the exact lines
    back (spacing inside lines is kept). Blank lines at either end are dropped.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return f"{marker} {SEPARATOR.join(lines)}" if lines else marker


def flag_marker(body: str) -> tuple[str, str, str] | None:
    """(label, reason, text after the marker) of a blocked or needs-human comment.

    Reads the one-line form and the older form with the marker on its own line.
    """
    match = FLAG_MARKER.match(body or "")
    return (match[1], match[2], body[match.end():].strip()) if match else None


def flag_text(body: str) -> tuple[str, str]:
    """(bold summary, evidence) of a flag comment in either form; the evidence keeps its lines."""
    match = FLAG_MARKER.match(body or "")
    rest = body[match.end():] if match else "\n" + body.partition("\n")[2]
    if rest.startswith(" "):  # One-line form: give the evidence its exact lines back.
        first, _, evidence = rest[1:].partition(SEPARATOR)
        evidence = evidence.replace(SEPARATOR, "\n")
    else:
        first, _, evidence = rest.strip().partition("\n")
        evidence = evidence.strip()
    first = first.strip()
    return first[2:-2] if first.startswith("**") and first.endswith("**") else first, evidence


def comment_order(comment: dict | object) -> tuple[datetime, int, int]:
    """Sort key for issue comments, oldest first, including queued ones.

    A comment queued in the GitHub outbox has id -<queue id>: it is newer than a
    delivered comment with the same time, and a larger queue id is newer.
    """
    created_at = comment["created_at"] if isinstance(comment, dict) else comment.created_at
    identity = comment["id"] if isinstance(comment, dict) else comment.id
    return datetime.fromisoformat(created_at), identity < 0, abs(identity)


def format_comment(
    reason: Reason, summary: str, details: str | None = None, *,
    label: Literal["blocked", "needs-human"] = "needs-human",
) -> str:
    summary = " ".join(summary.split())
    return one_line(f"<!-- foragent:{label} reason={reason.value} -->",
                    f"**{summary}**" + (f"\n{details}" if details else ""))


def parse_reason(comment_body: str) -> Reason | None:
    if (found := flag_marker(comment_body)) is None:
        return None
    try:
        return Reason(found[1])
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

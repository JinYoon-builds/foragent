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
REASON_CODE = r"[a-z-]+"


def is_reason_code(value: str) -> bool:
    """Validate the marker's wire grammar, including codes unknown to this version."""
    return re.fullmatch(REASON_CODE, value) is not None


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
    match = re.fullmatch(rf"<!-- foragent:(?:blocked|needs-human) reason=({REASON_CODE}) -->", first_line)
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


CONTRACT_MARKER = "<!-- foragent:contract"
_CONTRACT_HEAD = re.compile(
    re.escape(CONTRACT_MARKER) + r" issue=(\d+) body_sha=(\S+) by=(\S+) at=(\S+) build=(\S+) -->"
)


def _fenced(text: str) -> str:
    """A fence longer than any backtick run in text, so no line of text can close it."""
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", text))])
    return f"{fence}text\n{text}\n{fence}"


def format_contract_comment(
    number: int, body: str, body_sha: str, acceptance_blobs: dict[str, str],
    approved_by: str, approved_at: str, parent_context: str, build: str,
) -> str:
    """The build contract as public evidence: approval generation, locked checks, child body, parent."""
    lines = [
        f"{CONTRACT_MARKER} issue={number} body_sha={body_sha} by={approved_by} at={approved_at} build={build} -->",
        f"Build contract for #{number}: this build keeps the parent text it first read.", "",
        "Approved body:", _fenced(body), "", "Acceptance file blob hashes:",
        *(f"- `{path}`: `{sha}`" for path, sha in sorted(acceptance_blobs.items())), "",
        "Parent contract:", _fenced(parent_context),
    ]
    return "\n".join(lines)


def _take_fenced(lines: list[str], at: int) -> tuple[str, int] | None:
    """The fenced text opening at lines[at] and the index after its closing fence."""
    opening = re.fullmatch(r"(`{3,})text", lines[at]) if at < len(lines) else None
    if opening is None:
        return None
    for end in range(at + 1, len(lines)):
        if lines[end] == opening[1]:
            return "\n".join(lines[at + 1:end]), end + 1
    return None


def parse_contract_parent(comment_body: str, body_sha: str, approved_at: str, build: str) -> str | None:
    """The parent text of the contract comment of this build and approval generation, else None.

    Sections are read in order and each fenced block only to its own closing fence, so text
    inside the approved body or the parent can never pass for a section of the comment.
    """
    lines = comment_body.replace("\r\n", "\n").split("\n")
    head = _CONTRACT_HEAD.fullmatch(lines[0])
    if head is None or (head[2], head[4], head[5]) != (body_sha, approved_at, build):
        return None
    try:
        at = lines.index("Approved body:")
    except ValueError:
        return None
    approved = _take_fenced(lines, at + 1)
    if approved is None:
        return None
    at = approved[1]
    while at < len(lines) and lines[at] != "Parent contract:":
        if lines[at] and lines[at] != "Acceptance file blob hashes:" and not lines[at].startswith("- `"):
            return None
        at += 1
    parent = _take_fenced(lines, at + 1)
    if parent is None or any(line.strip() for line in lines[parent[1]:]):
        return None
    return parent[0]

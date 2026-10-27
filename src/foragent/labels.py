"""Shared issue labels installed by repository setup."""

from foragent.config import Config
from foragent.tracker import LabelSpec


APPROVED = "approved"
READY = "ready"
RUNNING = "running"
BLOCKED = "blocked"
NEEDS_HUMAN = "needs-human"
# Set only by `fa orch park --for-person`, removed only by `fa orch unpark`.
WAITING_PERSON = "waiting-person"
FEEDBACK = "feedback"
RISK_HIGH = "risk:high"


def issue_lane(labels, lanes) -> str:
    """Resolve one lane deterministically, falling back to the implicit main."""
    names = sorted(label.removeprefix("lane:") for label in labels if label.startswith("lane:"))
    name = names[0] if names else "main"
    return name if name in lanes else "main"


def lane_display(config: Config, slug: str, language: str) -> str:
    """Return the human lane name while keeping slugs stable for machines."""
    lane = config.lanes.get(slug, {})
    fallback = ("공통" if language.lower().split("-")[0] == "ko" else "Common") if slug == "main" else slug
    title = lane.get("title") or fallback
    emoji = lane.get("emoji") or ("🧰" if slug == "main" else "")
    return f"{emoji} {title}".strip()


def lane_order(name: str) -> tuple[bool, str]:
    return name == "main", name


def issue_size(labels) -> str:
    return next((size for size in ("s", "m", "l") if f"size:{size}" in labels), "m")


def attempt_tier(size: str, attempt: int) -> str:
    return ("s", "m", "l")[min(("s", "m", "l").index(size) + max(0, attempt - 1), 2)]


def state_group(issue, tracker) -> str:
    """The shared grouping used by gate status and order context."""
    if NEEDS_HUMAN in issue.labels:
        return NEEDS_HUMAN
    if RUNNING in issue.labels:
        return RUNNING
    if APPROVED in issue.labels and tracker.find_pr(f"foragent/issue-{issue.number}") is not None:
        return "pr-open"
    if READY in issue.labels:
        return READY
    return "approved-waiting" if APPROVED in issue.labels else "unapproved"

LABELS: list[LabelSpec] = [
    LabelSpec(APPROVED, "0e8a16", "Human approval of the issue body and acceptance checks."),
    LabelSpec(READY, "0075ca", "Approved and ready for a runner."),
    LabelSpec(RUNNING, "5319e7", "A runner is working on this issue."),
    LabelSpec(BLOCKED, "fbca04", "Waiting for the orchestrator to resolve a blocker."),
    LabelSpec(NEEDS_HUMAN, "d93f0b", "Human input is required before work can resume."),
    LabelSpec(WAITING_PERSON, "e4e669", "Waits for a person's decision"),
    LabelSpec(FEEDBACK, "fbca04", "Human feedback for future planning."),
    LabelSpec(RISK_HIGH, "b60205", "High risk changes require human review."),
    *(LabelSpec(f"size:{size}", color, f"Difficulty tier {size}.")
      for size, color in (("s", "c2e0c6"), ("m", "fef2c0"), ("l", "f9d0c4"))),
]

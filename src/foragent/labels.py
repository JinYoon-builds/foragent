"""Shared issue labels installed by repository setup."""

from foragent.tracker import LabelSpec


APPROVED = "approved"
READY = "ready"
RUNNING = "running"
NEEDS_HUMAN = "needs-human"
FEEDBACK = "feedback"
RISK_HIGH = "risk:high"

LABELS: list[LabelSpec] = [
    LabelSpec(APPROVED, "0e8a16", "Human approval of the issue body and acceptance checks."),
    LabelSpec(READY, "0075ca", "Approved and ready for a runner."),
    LabelSpec(RUNNING, "5319e7", "A runner is working on this issue."),
    LabelSpec(NEEDS_HUMAN, "d93f0b", "Human input is required before work can resume."),
    LabelSpec(FEEDBACK, "fbca04", "Human feedback for future planning."),
    LabelSpec(RISK_HIGH, "b60205", "High risk changes require human review."),
]

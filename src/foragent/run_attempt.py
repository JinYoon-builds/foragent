"""State and outcomes shared by the implementation loop steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.reasons import Reason

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.executors import Executor
    from foragent.git import GitRepo
    from foragent.issue import ParsedIssue
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.run_checks import CheckResult
    from foragent.run_review import ReviewResult
    from foragent.state import Approval
    from foragent.tracker import Issue


class PatchReplayError(GitError):
    """A child patch could not be replayed onto its selected destination."""


class AttemptFailure(Exception):
    """Checks failed and need another implementation attempt."""

    def __init__(self, message: str, target_branch: str = ""):
        super().__init__(message)
        self.target_branch = target_branch

    @property
    def failure(self) -> str:
        """The failure text for the next attempt, keeping a WholeFailure whole."""
        message = self.args[0] if self.args else ""
        return message if isinstance(message, WholeFailure) else str(self)


class ConflictBudgetExhausted(Exception):
    """Conflict resolution needs one fresh implementation on the latest target."""


class CIFailure(AttemptFailure):
    def __init__(self, logs: str, target_branch: str):
        super().__init__(f"CI failed:\n{logs}")
        self.target_branch = target_branch


class WholeFailure(str):
    """Failure text whose parts are already limited (each check tail, every review finding); kept whole."""


class ExecutorStopped(Exception):
    def __init__(self, outcome: str, detail: str = ""):
        super().__init__(detail)
        self.outcome = outcome


@dataclass
class Implementation:
    """The former closure state, retained across checks, reviews and merge retries."""

    n: int
    ctx: RunContext
    git: GitRepo
    approval: Approval
    parsed: ParsedIssue
    worktree: Path
    branch: str
    base_ref: str
    parent: int | None
    batch_branch: str | None
    target_branch: str
    start_ref: str
    approved_ref: str
    executor: Executor
    native_claude: bool
    options: dict
    log: Path
    issue: Issue
    size: str
    baseline: AcceptanceBaseline | None
    failure: str | CIFailure = ""
    reviews: list[ReviewResult] = field(default_factory=list)
    review_failures: int = 0
    review_hold: bool = False
    review_hold_reason: Reason = Reason.RISK_HIGH
    reviewer_text: str | None = None
    rebuttals: list[str] = field(default_factory=list)
    previous_rebutted_findings: set[str] = field(default_factory=set)
    checks: list[CheckResult] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    conflict_attempts: int = 0
    rebuilt: bool = False
    rebuild_lease: str | None = None
    pushed_head: str | None = None
    tracker_available: bool = True
    # Files this branch edits that the last merged base moved or split, told once.
    moved_files: list[str] = field(default_factory=list)

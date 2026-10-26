"""Frozen records persisted by the local state store."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Approval:
    number: int
    body: str
    body_sha: str
    acceptance_blobs: dict[str, str]
    approved_by: str
    approved_at: str


@dataclass(frozen=True)
class Flag:
    number: int
    reason: str
    flagged_at: str


@dataclass(frozen=True)
class InspectState:
    since: str | None = None
    notified_signature: str | None = None
    notified_items: list[str] = field(default_factory=list)
    summary_date: str | None = None


@dataclass(frozen=True)
class Run:
    number: int
    branch: str
    worktree: str
    pid: int | None
    started_at: str
    last_progress_at: str
    attempts: int = 1
    restarts: int = 0
    status: str = "running"
    outcome: str | None = None
    detail: str = ""
    lane: str = "main"
    waited_seconds: float = 0
    waiting_until: str | None = None
    transient_errors: int = 0
    fallback_model: str | None = None
    executor_waited_seconds: float = 0
    tier: str = "m"
    attempt_tiers: dict[str, str] = field(default_factory=dict)
    phase: str | None = None
    phase_started_at: str | None = None
    last_activity_at: str | None = None
    executor_pgid: int | None = None
    process_identities: list[dict] = field(default_factory=list)
    session_id: str | None = None
    session_executor: str | None = None
    session_ids: list[str] = field(default_factory=list)
    session_body: str | None = None
    resume_reason: str = ""
    resume_failures: int = 0
    merge_conflicts: list[str] = field(default_factory=list)
    # From-scratch rebuilds in this run and the fixed English machine reason for the latest one.
    rebuild_count: int = 0
    rebuild_reason: str | None = None
    local_skill_paths: list[str] = field(default_factory=list)
    approval_body_sha: str | None = None
    skills_reported: bool = False
    skills_used: list[str] = field(default_factory=list)
    last_review_pass_at: str | None = None
    review_pass_to_merge_seconds: float | None = None
    merge_lock_held_seconds: float | None = None
    comparison: dict | None = None
    files_committed: bool = False
    # A person's talk terminal is open (fa lanes talk); stall and runtime checks pause meanwhile.
    talking: bool = False
    talking_since: str | None = None
    # A runner wait (test slot, executor backoff, lock) open since this time, and the seconds it
    # overlapped the open talk: overlapping pauses count once (their union), not twice.
    wait_open_since: str | None = None
    talk_overlap_seconds: float = 0
    files_base_branch: str | None = None
    declared_files: list[str] | None = None
    changed_files: list[str] | None = None
    effective_files: list[str] | None = None
    late_files: list[str] = field(default_factory=list)
    overlap_records: list[dict] = field(default_factory=list)

    def talk_pause_seconds(self, now: datetime) -> float:
        """Seconds the open talk paused the run beyond what runner waits already credit.

        ``waited_seconds`` receives this when the talk ends; time when a runner wait
        was open too is credited by that wait, so it is left out here.
        """
        talking = _at(self.talking_since) if self.talking else None
        if talking is None:
            return 0.0
        return max(0.0, (now - talking).total_seconds() - self.talk_overlap_seconds - self.open_overlap(now))

    def open_overlap(self, now: datetime) -> float:
        """Seconds the open talk and the open runner wait have both been open, up to ``now``."""
        talking, waiting = _at(self.talking_since) if self.talking else None, _at(self.wait_open_since)
        if talking is None or waiting is None:
            return 0.0
        return max(0.0, (now - max(talking, waiting)).total_seconds())


def _at(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None

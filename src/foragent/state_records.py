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
    # A Foragent order parent's shared outcome and constraints, frozen once
    # per approval; None means not read yet, "" means there is none.
    parent_context: str | None = None


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
    # The saved failure is already limited part by part (every check, every finding): keep it whole.
    resume_whole: bool = False
    # Files this branch edits that a merged base moved or split, until the next prompt names them.
    moved_files: list[str] = field(default_factory=list)
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
    # Machine sleep credited by the gate tick alone (#415): over the whole run, and since
    # the last progress for the stall check. A record from before it reads as 0.
    slept_seconds: float = 0
    slept_since_progress: float = 0
    files_base_branch: str | None = None
    declared_files: list[str] | None = None
    changed_files: list[str] | None = None
    effective_files: list[str] | None = None
    late_files: list[str] = field(default_factory=list)
    overlap_records: list[dict] = field(default_factory=list)
    # One entry per related-unit-test check: changed_file_count, selected_files,
    # file_seconds (JUnit testcase seconds per file), total_seconds and timed_out.
    related_tests: list[dict] = field(default_factory=list)
    # Identity of the work the current phase does (e.g. "baseline:<sha>"), journaled when it ends;
    # work_events maps each finished work identity to its first phase event, across executions.
    phase_work_id: str | None = None
    # When this instance of the work began; segments split by nested waits share it.
    phase_work_started_at: str | None = None
    work_events: dict[str, str] = field(default_factory=dict)
    # A person's talk terminal is open (fa lanes talk); stall and runtime checks pause meanwhile.
    talking: bool = False
    talking_since: str | None = None
    # Open pauses (the talk and each runner wait: test slot, executor backoff, lock), by key,
    # with their start times; ``pause_mark`` is when their overlap was last counted.
    # ``waited_seconds`` always holds the union of closed pause time, never a sum (D55 addendum).
    pauses_open: dict[str, str] = field(default_factory=dict)
    pause_mark: str | None = None
    # The recorded session is not the running worker's (a fresh thread is starting, or it was
    # inherited from an earlier run and not resumed yet); nothing may be sent to it.
    session_stale: bool = False
    # ``slept_seconds`` at the first gate tick after the talk began (None before it). Sleep the
    # tick credits after that falls inside the talk, so the talk's own credit leaves it out; the
    # tick interval that straddles the talk's start may hold earlier sleep and is not subtracted.
    talk_slept_base: float | None = None

    def talk_pause_seconds(self, now: datetime) -> float:
        """Paused seconds not yet in ``waited_seconds``: the open talk, less pending overlap.

        Runner waits credit their own time as it passes; the talk credits its whole
        length when it ends. Time when several pauses are open at once counts once.
        """
        talking = _at(self.pauses_open.get("talk") or self.talking_since) if self.talking else None
        talk = max(0.0, (now - talking).total_seconds() - self.talk_slept()) if talking is not None else 0.0
        # May be negative: overlapping runner waits have already credited the shared time twice.
        return talk - self.pending_overlap(now)

    def talk_slept(self) -> float:
        """Machine sleep credited by ticks whose whole interval fell inside the open talk."""
        if not self.talking or self.talk_slept_base is None:
            return 0.0
        return max(0.0, self.slept_seconds - self.talk_slept_base)

    def pending_overlap(self, now: datetime) -> float:
        """Seconds counted more than once since ``pause_mark`` while several pauses are open."""
        mark = _at(self.pause_mark)
        if mark is None or len(self.pauses_open) < 2:
            return 0.0
        return (len(self.pauses_open) - 1) * max(0.0, (now - mark).total_seconds())


def _at(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None

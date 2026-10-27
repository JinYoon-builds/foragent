"""Frozen records persisted by the local state store."""

from dataclasses import dataclass, field


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
    # Paths the live worktree changed when an implementer turn ended: provenance for WIP deletions.
    live_changes: list[str] = field(default_factory=list)
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

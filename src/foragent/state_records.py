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
    # 'issue' runs implement an issue; a 'dream' run (number 0) tidies one lane's memory (#189).
    kind: str = "issue"

    @property
    def key(self) -> int | str:
        """The record's name in runs/: the issue number, or dream-<lane> for a dream run."""
        return f"dream-{self.lane}" if self.kind == "dream" else self.number
    # Identity of the work the current phase does (e.g. "baseline:<sha>"), journaled when it ends;
    # work_events maps each finished work identity to its first phase event, across executions.
    phase_work_id: str | None = None
    # When this instance of the work began; segments split by nested waits share it.
    phase_work_started_at: str | None = None
    work_events: dict[str, str] = field(default_factory=dict)
    # Only records from before #186: the implementer chosen at start; None meant the lane's executor.
    implementer: str | None = None
    # The concrete pair the tick chose at start (#186); the runner reads it and never chooses again.
    # A reviewer None means no review.
    executor: str | None = None
    reviewer: str | None = None
    # Runner waits as wall-clock regions: each open wait by key (its region as far as it has
    # credited), closed ones merged. ``waited_seconds`` changes by exactly the growth of their
    # union, so concurrent waits count once whatever order they credit in (D55 addendum).
    pause_regions: dict[str, list[str]] = field(default_factory=dict)
    pause_closed: list[list[str]] = field(default_factory=list)
    # The recorded session is not the running worker's (a fresh thread is starting, or it was
    # inherited from an earlier run and not resumed yet); nothing may be sent to it.
    session_stale: bool = False

    # A person's talk terminal is open (fa lanes talk): derived on every read from the talk
    # events by StateStore (D95), never stored; stall and runtime checks pause meanwhile.
    @property
    def talking(self) -> bool:
        return self.talking_since is not None

    @property
    def talking_since(self) -> str | None:
        return self.__dict__.get("_talk_since")

    def wait_regions(self) -> list[tuple[datetime, datetime]]:
        """Every runner wait's credited region (closed and open) as wall-clock pairs."""
        pairs = [*self.pause_closed, *self.pause_regions.values()]
        return [(start, end) for start, end in ((_at(a), _at(b)) for a, b in pairs) if start and end]

    def talk_pause_seconds(self, now: datetime) -> float:
        """Paused talk time that no runner wait credit already covers, derived up to ``now``.

        The talk regions come from the events (closed, superseded or open until ``now``),
        so this is never lost, never counted twice and never shrinks as time passes: the
        run's total pause is ``waited_seconds`` plus this, the length of the union.
        """
        talks = [(start, end or now) for start, end in self.__dict__.get("_talk_regions", ())]
        if not talks:
            return 0.0
        waits = self.wait_regions()
        return max(0.0, union_seconds([*waits, *talks]) - union_seconds(waits))


def union_seconds(regions) -> float:
    """Length of the union of ``(start, end)`` datetime pairs."""
    total, reach = 0.0, None
    for start, end in sorted((start, end) for start, end in regions if end > start):
        if reach is None or start > reach:
            total += (end - start).total_seconds()
            reach = end
        elif end > reach:
            total += (end - reach).total_seconds()
            reach = end
    return total


def _at(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None

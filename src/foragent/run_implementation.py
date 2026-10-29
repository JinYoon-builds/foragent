"""Prepare and execute one implementation turn with its lane context."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

from foragent import cuts
from foragent.actions import flag
from foragent.executors import ExecResult, classify_result
from foragent.issue import parse_body
from foragent.labels import attempt_tier, issue_size
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped, Implementation, WholeFailure
from foragent.run_baseline import _capture_acceptance_baseline
from foragent.run_checks import _append
from foragent.run_execution import _available_executor, _write_claude_settings
from foragent.run_lane_skills import lane_memory_notice, lane_notes, lane_skill_names, lane_skills
from foragent.run_skill_catalog import available_skills, catalog_section, parse_skills_used
from foragent.run_previous import (
    consume_hint, pending_hint, previous_attempt_summary, record_stop_point, saved_feedback_section,
)
from foragent.run_parent import parent_context
from foragent.run_prompts import bug_fix_steps, build_prompt, build_resume_prompt
from foragent.run_review import _read_rebuttals
from foragent.run_overlap import wait_for_overlap
from foragent.run_waits import _phase, _timeout, past_runtime_limit

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.runner import RunContext
    from foragent.git import GitRepo
    from foragent.run_baseline import AcceptanceBaseline


def lane_config(config: Config, lane: str) -> Config:
    """The configuration a run on this lane uses: the lane's executor and model win."""
    settings = config.lanes.get(lane, {}) if lane != "main" else {}
    reviewer = settings.get("reviewer") or config.reviewer
    return replace(
        config, executor=settings.get("executor") or config.executor,
        executor_model=settings.get("model") if settings.get("model") is not None else config.executor_model,
        # A lane-pinned reviewer wins; the global reviewer model belongs to the global reviewer only.
        reviewer=reviewer, reviewer_model=config.reviewer_model if reviewer == config.reviewer else None,
    )


@dataclass
class ImplementerSetup:
    """How an issue's implementer is started; shared by implementation and order preread."""

    executor: str
    model: str | None
    options: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    missing_skills: list[str] = field(default_factory=list)
    # What part of the lane memory an executor that loads it itself does not see.
    memory_notice: str = ""


def implementer_setup(
    config: Config, lane: str, tier: str, worktree: Path, full_text_dir: Path | None = None,
) -> ImplementerSetup:
    """Choose executor, model, lane agent, lane instructions and skills from a lane-adjusted config."""
    native_claude = config.executor == "claude"
    settings = config.tiers[tier]
    model = config.executor_model
    options: dict[str, Any] = {}
    if native_claude:
        model = (config.lanes.get(lane, {}) if lane != "main" else {}).get("model") or settings["claude_model"] or model
    else:
        options["effort"] = settings["codex_effort"]
    agent, notes = lane_notes(config, lane, worktree, full_text_dir)
    missing: list[str] = []
    if native_claude:
        # Claude reads the lane agent and memory itself; only a cut notice is added, after the task.
        if agent is not None:
            options["agent"] = agent
        memory_notice = lane_memory_notice(config, lane, worktree, full_text_dir) if agent is not None else ""
        notes = ""
    else:
        skill_notes, missing = lane_skills(config, lane, worktree, agent)
        notes += skill_notes
        memory_notice = ""
    return ImplementerSetup(config.executor, model, options, notes, missing, memory_notice)


def _record_skills_used(n: int, ctx: RunContext, result: ExecResult) -> None:
    """Keep every skill the implementer reported across attempts."""
    message = result.full_message if result.full_message is not None else result.last_message
    used = parse_skills_used(message or "")
    if used is None:
        return
    run = ctx.store.get_run(n)
    ctx.store.update_run(n, skills_reported=True, skills_used=list(dict.fromkeys([*run.skills_used, *used])))


_DETECTION_FILES = ("pyproject.toml", "package.json", "pnpm-lock.yaml", "go.mod")


def _detect_test_command_at(git: GitRepo, ref: str) -> str | None:
    """Detect the full-suite command from a base commit's files, not a PR checkout."""
    from foragent import runner

    sha = git.ref_sha(ref)
    with tempfile.TemporaryDirectory(prefix="foragent-detect-") as directory:
        root = Path(directory)
        for name in _DETECTION_FILES:
            if git.blob_sha(sha, name) is not None:
                (root / name).write_text(git._call(["show", f"{sha}:{name}"]), encoding="utf-8")
        return next(iter(runner.detect_test_commands(root)), None)


def capture_acceptance_baseline(
    n: int, ctx: RunContext, git: GitRepo, target_branch: str, ref: str,
    test_command: str | None = None,
) -> AcceptanceBaseline | None:
    """Capture (or reuse the cached) protected set for ``ref``; stop the run if unusable (#327).

    Run at preparation and again whenever the run adopts a newer base (#483).
    """
    from foragent import runner
    from foragent.ci_local import capture_local_only
    from foragent.ci_mode import enabled

    log = ctx.store.log_path(n)
    if test_command is None:
        test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(git.root)), None)
    try:
        started = ctx.monotonic()
        if enabled(ctx, target_branch):
            from foragent.run_baseline import _baseline_exclusions
            base_sha = git.ref_sha(ref)
            baseline = capture_local_only(n, ctx, git, test_command, base_sha,
                                          _baseline_exclusions(ctx, base_sha, log))
            return replace(baseline, test_command=test_command) if baseline else None
        baseline = _capture_acceptance_baseline(n, ctx, test_command, ref)
        ctx.store.update_run(n, comparison={"source": "local", "duration_seconds": max(0, ctx.monotonic() - started)})
        return replace(baseline, test_command=test_command) if baseline else None
    except AttemptFailure as exc:
        _baseline_unavailable(n, ctx, str(exc), exc)


def _baseline_unavailable(n: int, ctx: RunContext, detail: str, cause: Exception | None = None) -> None:
    """The #327 stop: no merge without a usable protected set."""
    _append(ctx.store.log_path(n), detail)
    reason = Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    flag(ctx, n, reason, "acceptance baseline unavailable", detail)
    raise ExecutorStopped(reason.value, detail) from cause


# Verification runs no baseline selectors once a base sync leaves none protected (#123).
EMPTIED = "Acceptance baseline verification skipped (count=0): no protected node ids remain"


def follow_base(attempt: Implementation) -> None:
    """Keep the protected set on the base the branch is built on (#483).

    Every step that adopts a newer base (a result-affecting move, a rebuild,
    a switch between batch and base targets) changes ``start_ref``. Checks
    call this before verifying, so none of them can verify against the
    protected set and exclusions of an older base. The command is the one the
    protected set was captured with: the branch may have changed the files
    test-command detection reads.
    """
    if getattr(attempt, "baseline_sha", None) is None:  # Partial attempts carry no base to follow.
        return
    sha = attempt.git.ref_sha(attempt.start_ref)
    if sha == attempt.baseline_sha:
        return
    previous = attempt.baseline
    if previous is not None and not previous.node_ids:
        # An empty protected set stays empty for the run: nothing to re-capture (#123).
        attempt.baseline_sha = sha
        return
    owned: set[str] = set()
    missing: set[str] = set()
    if previous is not None and previous.node_ids:
        # Collect the protected ids on the new base first; an id it does not
        # collect drops as missing, and none left skips the re-capture (#123).
        from foragent.run_baseline import collect_protected_ids, log_dropped_ids
        try:
            owned, missing = collect_protected_ids(attempt.n, attempt.ctx, previous, sha)
        except AttemptFailure as exc:
            # The run stops (#327) and drops nothing. #483 still measures the new
            # base once; its unusable capture usually stops the run first.
            capture_acceptance_baseline(attempt.n, attempt.ctx, attempt.git, attempt.target_branch, sha,
                                        previous.test_command)
            _baseline_unavailable(attempt.n, attempt.ctx, str(exc), exc)
        if not set(previous.node_ids) - owned - missing:
            log_dropped_ids(attempt.log, previous, (), owned, missing, None, False)
            _append(attempt.log, "Acceptance baseline re-capture skipped: no protected node ids remain")
            _append(attempt.log, EMPTIED)
            attempt.baseline = replace(previous, node_ids=(), collected_ids=(), failing_ids=(), base_sha=sha)
            attempt.baseline_sha = sha
            return
    # Without an earlier protected set, the worktree holds the PR: read the base.
    command = previous.test_command if previous else (
        attempt.ctx.config.test_command or _detect_test_command_at(attempt.git, sha))
    baseline = capture_acceptance_baseline(attempt.n, attempt.ctx, attempt.git, attempt.target_branch, sha, command)
    if baseline is None and previous is not None:
        # A run that had protection must not continue with none (#327).
        _baseline_unavailable(attempt.n, attempt.ctx,
                              f"acceptance baseline unavailable: no baseline could be captured for the new base {sha}")
    if previous is not None and previous.node_ids:
        from foragent.ci_mode import enabled
        from foragent.run_baseline import log_dropped_ids
        log_dropped_ids(attempt.log, previous, baseline.node_ids, owned, missing, baseline,
                        enabled(attempt.ctx, attempt.target_branch))
        if not baseline.node_ids:
            # Every survivor fails on the new base: as final as protection lost by collection.
            _append(attempt.log, EMPTIED)
    attempt.baseline, attempt.baseline_sha = baseline, sha


def _prepare_implementation(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
    capture_baseline: bool = True,
) -> Implementation:
    """Set up an implementer; a throwaway doctor task has no acceptance baseline to keep."""
    from foragent import runner

    approval = parent_context(ctx, n, ctx.store.load_approval(n))
    parsed = parse_body(approval.body)
    worktree = git.root
    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    start_ref = f"origin/{target_branch}"
    approved_ref = start_ref
    executor = ctx.executor_factory(ctx.config.executor)
    native_claude = ctx.config.executor == "claude"
    options = {
        "settings_path": _write_claude_settings(n, ctx, worktree),
        "goal": runner.goal_condition(ctx.config.acceptance_dir),
    } if native_claude else {}
    log = ctx.store.log_path(n)
    issue = ctx.tracker.get_issue(n)
    size = issue_size(issue.labels)
    baseline_sha = git.ref_sha(start_ref) if capture_baseline else None
    baseline_command = ctx.config.test_command
    if baseline_command is None and capture_baseline and git.head_sha() != baseline_sha:
        # A resumed run's worktree holds its saved branch; measure the base by the base's files.
        baseline_command = _detect_test_command_at(git, baseline_sha)
    baseline = (capture_acceptance_baseline(n, ctx, git, target_branch, baseline_sha, baseline_command)
                if capture_baseline else None)
    return Implementation(
        n=n,
        ctx=ctx,
        git=git,
        approval=approval,
        parsed=parsed,
        worktree=worktree,
        branch=branch,
        base_ref=base_ref,
        parent=parent,
        batch_branch=batch_branch,
        target_branch=target_branch,
        start_ref=start_ref,
        approved_ref=approved_ref,
        executor=executor,
        native_claude=native_claude,
        options=options,
        log=log,
        issue=issue,
        size=size,
        baseline=baseline,
        baseline_sha=baseline_sha,
        failure=failure,
    )


def _implement(
    attempt: Implementation,
    failure: str | CIFailure, *, merge_conflicts: list[str] | None = None, reviewer_text: str | None = None,
) -> None:
    from foragent import runner

    n, ctx = attempt.n, attempt.ctx
    wait_for_overlap(attempt)
    worktree, log = attempt.worktree, attempt.log
    _phase(n, ctx, "resolving-conflict" if merge_conflicts is not None else "implementing")
    remaining = runner._remaining(n, ctx)
    # The step just finished was stopped (a check the watchdog found silent, or a review
    # session that timed out): past max_runtime_minutes, the run ends here (#435).
    stopped = (any(check.timed_out for check in attempt.checks)
               or bool(getattr(attempt, "reviews", None)) and attempt.reviews[-1].timed_out)
    if remaining <= 0 or (stopped and past_runtime_limit(n, ctx)):
        raise ExecutorStopped(_timeout(n, ctx))
    run = ctx.store.get_run(n)
    tier = attempt_tier(attempt.size, run.attempts)
    ctx.store.update_run(n, tier=tier, attempt_tiers={**run.attempt_tiers, str(run.attempts): tier})
    full_text_dir = cuts.issue_dir(ctx.store.root, n)
    setup = implementer_setup(ctx.config, run.lane, tier, worktree, full_text_dir)
    model, notes = setup.model, setup.notes
    attempt.options.update(setup.options)
    attempt.missing_skills.extend(name for name in setup.missing_skills if name not in attempt.missing_skills)
    retry_default, hint = pending_hint(ctx.store.root, n)
    failure_context = str(failure) or run.resume_reason
    # Each part of a check bundle or review is already limited; a saved one stays whole after restarts too.
    whole = isinstance(failure, (CIFailure, WholeFailure)) or (not str(failure) and run.resume_whole)
    moved_files = run.moved_files
    # The saved review and failing checks reach every new attempt, fresh or resumed.
    saved_feedback = saved_feedback_section(ctx.store.root, n, failure_context)
    if str(failure):
        # Save this turn's feedback first: a limit wait or restart before delivery must not lose it.
        ctx.store.update_run(n, resume_reason=failure_context, resume_whole=whole)
    review_feedback = attempt.review_feedback
    attempt.review_feedback = False
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    # Bug-fix steps reach implementation turns (first, retries, resumes), not conflict resolution.
    procedure = bug_fix_steps(attempt.issue.labels, worktree) if merge_conflicts is None else None
    fixed = lane_skill_names(ctx.config, run.lane, worktree, lane_notes(ctx.config, run.lane, worktree)[0])
    prompt = build_prompt(
        n, attempt.parsed, ctx.config.acceptance_dir, failure_context, ctx.config.language,
        native_goal=not attempt.native_claude and ctx.config.codex_goal, acceptance_locked=attempt.native_claude,
        merge_conflicts=merge_conflicts,
        reviewer_text=reviewer_text,
        orchestrator_hint=hint,
        test_command=test_command,
        acceptance_baseline=attempt.baseline.node_ids if attempt.baseline else (),
        failure_limit=None if whole else 4000,
        parent_context=attempt.approval.parent_context or "",
        full_text_dir=full_text_dir,
        moved_files=moved_files,
        skill_catalog=catalog_section(available_skills(ctx.config, fixed)),
        previous_attempt=(previous_attempt_summary(attempt, failure_context)
                          if failure_context or reviewer_text is not None or saved_feedback else ""),
        review_feedback=review_feedback,
        retry_default=retry_default,
        procedure=procedure,
    )
    offset = log.stat().st_size if log.exists() else 0
    result = None
    try:
        result = _available_executor(
            n, ctx, attempt.executor, worktree=worktree,
            prompt=notes + prompt + setup.memory_notice,
            session_body=attempt.approval.body,
            resume_prompt=build_resume_prompt(
                n, run.session_body, attempt.approval.body, failure_context,
                reviewer_text=reviewer_text, orchestrator_hint=hint, merge_conflicts=merge_conflicts,
                review_feedback=review_feedback, retry_default=retry_default,
                moved_files=moved_files, parent_context=attempt.approval.parent_context or "",
                procedure=procedure,
            ) + (f"\n\n{saved_feedback}" if saved_feedback else "") + setup.memory_notice,
            model=model, timeout_s=remaining, log_path=log, **attempt.options,
        )
    finally:
        # Every way a turn ends (result, limit wait, launch failure, interruption) leaves its stop point.
        try:
            record_stop_point(ctx.store.root, n, result, log, offset)
        except OSError:
            pass
    # A prepared prompt has not been delivered yet. Keep its hint through
    # availability waits, launch failures and interruptions; acknowledge it
    # only once the implementer returns an ordinary execution result.
    if (retry_default or hint is not None) and classify_result(result) == "normal":
        consume_hint(ctx.store.root, n)
    if moved_files and classify_result(result) == "normal":
        ctx.store.update_run(n, moved_files=[])  # named once, after delivery
    _record_skills_used(n, ctx, result)
    rebuttal = _read_rebuttals(worktree)
    if rebuttal.strip() and rebuttal not in attempt.rebuttals:
        attempt.rebuttals.append(rebuttal)
    if result.timed_out or runner._remaining(n, ctx) <= 0:
        raise ExecutorStopped(_timeout(n, ctx))
    question = worktree / ".foragent/QUESTION.md"
    if question.exists():
        details = cuts.head(question.read_text(encoding="utf-8", errors="replace"), 6000,
                            cuts.issue_dir(ctx.store.root, n), "QUESTION.md")
        flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
        raise ExecutorStopped("spec-question")
    wait_for_overlap(attempt)

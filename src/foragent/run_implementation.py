"""Prepare and execute one implementation turn with its lane context."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

from foragent.actions import flag
from foragent.executors import ExecResult, classify_result
from foragent.issue import parse_body
from foragent.labels import attempt_tier, issue_size
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped, Implementation
from foragent.run_baseline import _capture_acceptance_baseline
from foragent.run_checks import _append
from foragent.run_execution import _available_executor, _write_claude_settings
from foragent.run_lane_skills import lane_notes, lane_skill_names, lane_skills
from foragent.run_skill_catalog import available_skills, catalog_section, parse_skills_used
from foragent.run_prompts import build_prompt, build_resume_prompt
from foragent.run_review import _read_rebuttals
from foragent.run_waits import _phase, _timeout

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.runner import RunContext
    from foragent.git import GitRepo
    from foragent.run_baseline import AcceptanceBaseline


def lane_config(config: Config, lane: str) -> Config:
    """The configuration a run on this lane uses: the lane's executor and model win."""
    settings = config.lanes.get(lane, {}) if lane != "main" else {}
    return replace(
        config, executor=settings.get("executor") or config.executor,
        executor_model=settings.get("model") if settings.get("model") is not None else config.executor_model,
    )


@dataclass
class ImplementerSetup:
    """How an issue's implementer is started; shared by implementation and order preread."""

    executor: str
    model: str | None
    options: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    missing_skills: list[str] = field(default_factory=list)


def implementer_setup(config: Config, lane: str, tier: str, worktree: Path) -> ImplementerSetup:
    """Choose executor, model, lane agent, lane instructions and skills from a lane-adjusted config."""
    native_claude = config.executor == "claude"
    settings = config.tiers[tier]
    model = config.executor_model
    options: dict[str, Any] = {}
    if native_claude:
        model = (config.lanes.get(lane, {}) if lane != "main" else {}).get("model") or settings["claude_model"] or model
    else:
        options["effort"] = settings["codex_effort"]
    agent, notes = lane_notes(config, lane, worktree)
    missing: list[str] = []
    if native_claude:
        # Claude reads the lane agent itself; its notes are not prepended.
        if agent is not None:
            options["agent"] = agent
        notes = ""
    else:
        skill_notes, missing = lane_skills(config, lane, worktree, agent)
        notes += skill_notes
    return ImplementerSetup(config.executor, model, options, notes, missing)


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
    """Detect the full-suite command from a commit's files, not the checkout."""
    from foragent import runner

    sha = git.ref_sha(ref)
    with tempfile.TemporaryDirectory(prefix="foragent-detect-") as directory:
        root = Path(directory)
        for name in _DETECTION_FILES:
            if git.blob_sha(sha, name) is not None:
                (root / name).write_text(git._call(["show", f"{sha}:{name}"]), encoding="utf-8")
        return next(iter(runner.detect_test_commands(root)), None)


def capture_acceptance_baseline(
    n: int, ctx: RunContext, git: GitRepo, target_branch: str, ref: str, *, detect_at_ref: bool = False,
    required: bool = False,
) -> AcceptanceBaseline | None:
    """Capture (or reuse the cached) protected set for ``ref``; stop the run if unusable (#327).

    Run at preparation and again after each result-affecting base move (#483).
    A refresh detects the test command from ``ref`` itself: the worktree then
    holds the PR, which must not decide how the new base is measured.
    ``required`` (a refresh of a run that already had a protected set) turns
    "no baseline" into the #327 stop instead of silently dropping protection.
    """
    from foragent import runner
    from foragent.ci_local import capture_local_only
    from foragent.ci_mode import enabled

    log = ctx.store.log_path(n)
    test_command = ctx.config.test_command
    if test_command is None:
        if detect_at_ref:
            test_command = _detect_test_command_at(git, ref)
        else:
            test_command = next(iter(runner.detect_test_commands(git.root)), None)
    try:
        started = ctx.monotonic()
        if enabled(ctx, target_branch):
            from foragent.run_baseline import _baseline_exclusions
            base_sha = git.ref_sha(ref)
            return capture_local_only(n, ctx, git, test_command, base_sha,
                                      _baseline_exclusions(ctx, base_sha, log))
        baseline = _capture_acceptance_baseline(n, ctx, test_command, ref)
        if baseline is None and required:
            raise AttemptFailure(
                f"acceptance baseline unavailable: no baseline could be captured for the new base {ref}")
        ctx.store.update_run(n, comparison={"source": "local", "duration_seconds": max(0, ctx.monotonic() - started)})
        return baseline
    except AttemptFailure as exc:
        _append(log, str(exc))
        reason = Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
        flag(ctx, n, reason, "acceptance baseline unavailable", str(exc))
        raise ExecutorStopped(reason.value, str(exc)) from exc


def follow_base(attempt: Implementation) -> None:
    """Keep the protected set on the base the branch is built on (#483).

    Every step that adopts a newer base (a result-affecting move, a rebuild,
    a switch between batch and base targets) changes ``start_ref``. Checks
    call this before verifying, so none of them can verify against the
    protected set and exclusions of an older base.
    """
    if attempt.baseline_sha is None:
        return
    sha = attempt.git.ref_sha(attempt.start_ref)
    if sha == attempt.baseline_sha:
        return
    attempt.baseline = capture_acceptance_baseline(
        attempt.n, attempt.ctx, attempt.git, attempt.target_branch, sha,
        detect_at_ref=True, required=attempt.baseline is not None)
    attempt.baseline_sha = sha


def _prepare_implementation(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
    capture_baseline: bool = True,
) -> Implementation:
    """Set up an implementer; a throwaway doctor task has no acceptance baseline to keep."""
    from foragent import runner

    approval = ctx.store.load_approval(n)
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
    baseline = (capture_acceptance_baseline(n, ctx, git, target_branch, baseline_sha)
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
    worktree, log = attempt.worktree, attempt.log
    _phase(n, ctx, "resolving-conflict" if merge_conflicts is not None else "implementing")
    remaining = runner._remaining(n, ctx)
    if remaining <= 0:
        raise ExecutorStopped(_timeout(n, ctx))
    run = ctx.store.get_run(n)
    tier = attempt_tier(attempt.size, run.attempts)
    ctx.store.update_run(n, tier=tier, attempt_tiers={**run.attempt_tiers, str(run.attempts): tier})
    setup = implementer_setup(ctx.config, run.lane, tier, worktree)
    model, notes = setup.model, setup.notes
    attempt.options.update(setup.options)
    attempt.missing_skills.extend(name for name in setup.missing_skills if name not in attempt.missing_skills)
    hint_path = ctx.store.root / "hints" / f"{n}.txt"
    hint = hint_path.read_text(encoding="utf-8") if hint_path.is_file() else None
    failure_context = str(failure) or run.resume_reason
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    fixed = lane_skill_names(ctx.config, run.lane, worktree, lane_notes(ctx.config, run.lane, worktree)[0])
    prompt = build_prompt(
        n, attempt.parsed, ctx.config.acceptance_dir, failure_context, ctx.config.language,
        native_goal=not attempt.native_claude and ctx.config.codex_goal, acceptance_locked=attempt.native_claude,
        merge_conflicts=merge_conflicts,
        reviewer_text=reviewer_text,
        orchestrator_hint=hint,
        test_command=test_command,
        acceptance_baseline=attempt.baseline.node_ids if attempt.baseline else (),
        failure_limit=None if isinstance(failure, CIFailure) else 4000,
        skill_catalog=catalog_section(available_skills(ctx.config, fixed)),
    )
    result = _available_executor(
        n, ctx, attempt.executor, worktree=worktree,
        prompt=prompt if attempt.native_claude else notes + prompt,
        session_body=attempt.approval.body,
        resume_prompt=build_resume_prompt(
            n, run.session_body, attempt.approval.body, failure_context,
            reviewer_text=reviewer_text, orchestrator_hint=hint, merge_conflicts=merge_conflicts,
        ),
        model=model, timeout_s=remaining, log_path=log, **attempt.options,
    )
    # A prepared prompt has not been delivered yet. Keep its hint through
    # availability waits, launch failures and interruptions; acknowledge it
    # only once the implementer returns an ordinary execution result.
    if hint is not None and classify_result(result) == "normal":
        hint_path.unlink()
    _record_skills_used(n, ctx, result)
    rebuttal = _read_rebuttals(worktree)
    if rebuttal.strip() and rebuttal not in attempt.rebuttals:
        attempt.rebuttals.append(rebuttal)
    if result.timed_out or runner._remaining(n, ctx) <= 0:
        raise ExecutorStopped(_timeout(n, ctx))
    question = worktree / ".foragent/QUESTION.md"
    if question.exists():
        with question.open(encoding="utf-8", errors="replace") as stream:
            details = stream.read(6000)
        flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
        raise ExecutorStopped("spec-question")

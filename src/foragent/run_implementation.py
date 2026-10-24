"""Prepare and execute one implementation turn with its lane context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent.actions import flag
from foragent.executors import classify_result
from foragent.issue import parse_body
from foragent.labels import attempt_tier, issue_size
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped, Implementation
from foragent.run_baseline import _capture_acceptance_baseline
from foragent.run_checks import _append
from foragent.run_execution import _available_executor, _write_claude_settings
from foragent.run_lane_skills import _lane_notes, _lane_skills
from foragent.run_prompts import build_prompt, build_resume_prompt
from foragent.run_review import _read_rebuttals
from foragent.run_waits import _phase, _timeout

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.git import GitRepo


def _prepare_implementation(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
) -> Implementation:
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
    baseline_test_command = ctx.config.test_command
    if baseline_test_command is None:
        baseline_test_command = next(iter(runner.detect_test_commands(worktree)), None)
    try:
        baseline = _capture_acceptance_baseline(n, ctx, baseline_test_command, start_ref)
    except AttemptFailure as exc:
        _append(log, str(exc))
        reason = Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
        flag(ctx, n, reason, "acceptance baseline unavailable", str(exc))
        raise ExecutorStopped(reason.value, str(exc)) from exc
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
    settings = ctx.config.tiers[tier]
    lane = ctx.config.lanes.get(run.lane, {})
    model = ctx.config.executor_model
    if attempt.native_claude:
        model = lane.get("model") or settings["claude_model"] or model
    else:
        attempt.options["effort"] = settings["codex_effort"]
    agent, notes = _lane_notes(ctx, n, worktree)
    if attempt.native_claude and agent is not None:
        attempt.options["agent"] = agent
    if not attempt.native_claude:
        skill_notes, missing = _lane_skills(ctx, n, worktree, agent)
        notes += skill_notes
        attempt.missing_skills.extend(name for name in missing if name not in attempt.missing_skills)
    hint_path = ctx.store.root / "hints" / f"{n}.txt"
    hint = hint_path.read_text(encoding="utf-8") if hint_path.is_file() else None
    failure_context = str(failure) or run.resume_reason
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    prompt = build_prompt(
        n, attempt.parsed, ctx.config.acceptance_dir, failure_context, ctx.config.language,
        native_goal=not attempt.native_claude and ctx.config.codex_goal, acceptance_locked=attempt.native_claude,
        merge_conflicts=merge_conflicts,
        reviewer_text=reviewer_text,
        orchestrator_hint=hint,
        test_command=test_command,
        acceptance_baseline=attempt.baseline.node_ids if attempt.baseline else (),
        failure_limit=None if isinstance(failure, CIFailure) else 4000,
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

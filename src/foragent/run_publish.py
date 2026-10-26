"""Verified PR contents, publication, CI and merge policy."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from foragent import board
from foragent.actions import FlagContext, flag, invalidate_approval
from foragent.ci_mode import ComparisonEvidence, enabled, require_comparison, stage_exclusions
from foragent.git import GitError, GitRepo, PublicationError
from foragent.issue import ParsedIssue, body_hash
from foragent.labels import APPROVED, BLOCKED, READY, RUNNING
from foragent.policy import Decision, merge_decision, review_mode_for, touched
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped
from foragent.run_base_move import BaseMove, REUSED, REVERIFY, base_move_is_neutral, capture_move, fetch_move
from foragent.run_checks import _acceptance_matches, _append
from foragent.run_prompts import review_scope_rules
from foragent.run_skill_catalog import report_line
from foragent.run_waits import _credited_wait, _phase, _progress, _timeout
from foragent.state import Approval, Run
from foragent.tracker import TrackerError

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.run_attempt import Implementation
    from foragent.run_checks import CheckResult
    from foragent.run_review import ReviewResult


def _pr_body(
    n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int,
    reviews: list[ReviewResult], try_text: str | None = None,
    *, outside_lane: list[str] | None = None, fallback_model: str | None = None,
    attempt_tiers: dict[str, str] | None = None,
    missing_skills: list[str] | None = None,
    skills_line: str | None = None,
    rebuttals: str = "",
    review_scope: str = "claude-local", acceptance_dir: str = "tests/acceptance/",
) -> str:
    lines = [f"Closes #{n}", "", "| Done when command | Result |", "| --- | --- |"]
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        lines.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
    lines.extend(["", "## Review", ""])
    if review_scope == "pr-body":
        lines.append(f"VERDICT: {reviews[-1].verdict}" if reviews else "Pending review.")
        lines.extend(["", "## Review scope", "", review_scope_rules(acceptance_dir),
                      "The acceptance tests are locked."])
    elif not reviews:
        lines.append("Disabled (reviewer: none).")
    for round_number, review in enumerate(reviews if review_scope == "claude-local" else [], 1):
        lines.append(f"Round {round_number}: VERDICT: {review.verdict}")
        if review.findings:
            lines.append(review.findings)
        lines.extend(review.notes)
        lines.append("")
    if rebuttals:
        lines.extend(["", "## Rebuttals", "", rebuttals])
    if parsed.done_when_checklist:
        lines.extend(["", "## Needs a human check", ""])
        lines.extend(f"- [ ] {item.text}" for item in parsed.done_when_checklist)
    if outside_lane:
        lines.extend(["", "## Outside lane", "", *(f"- `{path}`" for path in outside_lane)])
    if missing_skills:
        lines.extend(["", "## Lane skills", "", *(f"- missing skill: {name}" for name in missing_skills)])
    if skills_line is not None:
        lines.extend(["", skills_line])
    if fallback_model is not None:
        lines.extend(["", f"Executor fallback model: {fallback_model}"])
    if attempt_tiers:
        lines.extend(["", *(f"Attempt {number} at tier {tier}" for number, tier in attempt_tiers.items())])
    lines.extend(["", f"Opened by Foragent gate (attempt {attempt})"])
    return "\n".join(lines) + "\n\n## Try it\n\n" + (
        try_text if try_text is not None else "_not provided by the implementer_"
    )


def _hold_changed_head(n: int, ctx: RunContext, link: str) -> str:
    flag(ctx, n, Reason.RISK_HIGH, "PR head changed after verification", link)
    return "held"


def _hold_if_not_started(n: int, ctx: RunContext, pr: int, logs: str) -> None:
    """Jobs that never ran are infrastructure: hold with the evidence, keep the PR open."""
    if not getattr(logs, "not_started", False):
        return
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    _append(ctx.store.log_path(n), f"CI did not start:\n{logs}")
    flag(ctx, n, Reason.CI_NOT_STARTED,
         "CI jobs did not start; there is no code failure to repair.", f"{logs}\n\n{link}")
    raise ExecutorStopped("held", str(logs))


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    logs = ctx.tracker.pr_failed_logs(pr)
    failure = CIFailure(logs, target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise ExecutorStopped(_hold_changed_head(n, ctx, link))
    _hold_if_not_started(n, ctx, pr, logs)
    run = ctx.store.get_run(n)
    _append(ctx.store.log_path(n), f"Attempt {run.attempts} failed:\n{failure}")
    if run.attempts >= ctx.config.max_retries + 1:
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "CI failed after all attempts.", f"{failure}\n\n{link}")
        raise ExecutorStopped("ci-failed", str(failure))
    ctx.store.update_run(n, attempts=run.attempts + 1)
    raise failure


@contextmanager
def _merge_lock(n: int, ctx: RunContext):
    # Keep tick informed during an unbounded blocking flock acquisition.
    started = None
    try:
        with ExitStack() as stack:
            def acquire():
                nonlocal started
                stack.enter_context(ctx.store.merge_lock())
                started = ctx.now()

            _credited_wait(n, ctx, acquire)
            yield
    finally:
        if started is not None:
            ctx.store.record_merge_lock(n, (ctx.now() - started).total_seconds())


def _ci_status(ctx: RunContext, pr: int, worktree: Path, ready_at: float) -> str:
    status = ctx.tracker.pr_checks(pr)
    has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                        for path in (worktree / ".github/workflows").glob(pattern))
    if status == "none" and has_workflows and ctx.now().timestamp() - ready_at < 180:
        return "pending"
    return status


@dataclass
class _CIWait:
    started: float
    polls: int = 0
    status: str = "pending"
    infrastructure: str = ""

    @property
    def deadline(self) -> float:
        return self.started + 1200


def _wait_for_ci(
    n: int, ctx: RunContext, git: GitRepo, pr: int, head: str, ready_at: float,
    wait: _CIWait, failed: Callable[[], None], status: str | None = None,
    *, comparison_mode: bool = False,
) -> str | None:
    """Consume even a status observed under the lock, but only wait unlocked."""
    from foragent import runner

    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    wait.infrastructure = ""
    while True:
        if runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if status is None:
            status = _ci_status(ctx, pr, git.root, ready_at)
        wait.status = status
        _progress(n, ctx)
        if ctx.tracker.pr_head_sha(pr) != head:
            return _hold_changed_head(n, ctx, link)
        if status in {"pass", "none"}:
            return None
        if status == "fail":
            if comparison_mode:
                evidence = ctx.tracker.pr_failed_logs(pr)
                if getattr(evidence, "not_started", False):
                    wait.infrastructure = str(evidence)
                    return None
            failed()
        remaining = wait.deadline - ctx.now().timestamp()
        if remaining <= 0 or wait.polls >= 40:
            if comparison_mode and ctx.config.checks_fallback == "local":
                wait.infrastructure = "CI did not finish before the poll deadline."
                return None
            flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
            return "timeout"
        ctx.sleep(min(30, remaining, runner._remaining(n, ctx)))
        wait.polls += 1
        status = None


def _policy_allows(decision: Decision, base: str, batch_branch: str | None) -> bool:
    return decision.kind == "merge" or (
        decision.kind == "hold_mode" and decision.mode != "careful" and base == batch_branch
    )


@dataclass
class _MergeInputs:
    # A stop is deferred so flagging/invalidation happens after releasing the lock.
    stop: Callable[[], str] | None = None
    base_sha: str = ""
    paths: list[str] = field(default_factory=list)
    decision: Decision = Decision("merge")
    status: str = "pending"
    batch_branch: str | None = None
    comparison_current: bool = True


def _final_merge_inputs(
    n: int, ctx: RunContext, git: GitRepo, pr: int, branch: str, base: str,
    head: str, approval: Approval, ready_at: float, *, base_sha: str | None = None,
    approved_body_sha: str | None = None,
    comparison: ComparisonEvidence | None = None,
) -> _MergeInputs:
    """Read every mutable prerequisite after acquiring the final merge lock.

    Re-read approval/labels (and live body for review-only), PR state/branches, local and remote head,
    fetched target SHA, approved acceptance blobs on head and configured base,
    local and tracker paths, current policy config and batch parent, comparison evidence,
    CI (including workflow registration grace), and the remaining run budget. Verification/review
    evidence stays pinned to the supplied head and approval snapshot.
    """
    link = f"https://github.com/{ctx.repo}/pull/{pr}"

    def invalid_approval() -> str:
        invalidate_approval(ctx, n, "Approval invalidated: issue, approval or acceptance files changed. Re-approve to run it.")
        return "hash-mismatch"

    def changed_base() -> str:
        flag(ctx, n, Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
        return "base-changed"

    status = _ci_status(ctx, pr, git.root, ready_at)
    issue = ctx.tracker.get_issue(n)
    saved = ctx.store.load_approval(n)
    if (APPROVED not in issue.labels
            or (approved_body_sha is not None and body_hash(issue.body) != approved_body_sha)
            or saved is None or saved.body_sha != approval.body_sha
            or saved.acceptance_blobs != approval.acceptance_blobs):
        return _MergeInputs(stop=invalid_approval)
    current = next((item for item in ctx.tracker.list_prs("open", branch) if item.number == pr), None)
    if current is None or current.head != branch or current.base != base:
        return _MergeInputs(stop=changed_base)
    parent = ctx.tracker.parent_of(n)
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    if base not in {ctx.config.base_branch, batch_branch}:
        return _MergeInputs(stop=changed_base)
    ctx.git.fetch("origin", base)
    target_sha = git.ref_sha(f"origin/{base}")
    if base_sha is not None and target_sha != base_sha:
        return _MergeInputs(stop=changed_base)
    configured_base_sha = target_sha
    if base != ctx.config.base_branch:
        ctx.git.fetch("origin", ctx.config.base_branch)
        configured_base_sha = git.ref_sha(f"origin/{ctx.config.base_branch}")
    for ref in (head, configured_base_sha):
        blobs = git.blob_shas(ref, approval.acceptance_blobs)
        if any(blobs.get(path) != sha for path, sha in approval.acceptance_blobs.items()):
            return _MergeInputs(stop=invalid_approval)
    paths = sorted(set(git.changed_paths(target_sha)) | set(ctx.tracker.pr_changed_files(pr)))
    decision = merge_decision(changed_paths=paths, issue_labels=issue.labels, config=ctx.config)
    # Bind this status and all verification evidence to the exact head we merge.
    if git.head_sha() != head or ctx.tracker.pr_head_sha(pr) != head:
        return _MergeInputs(stop=lambda: _hold_changed_head(n, ctx, link))
    from foragent import runner
    if runner._remaining(n, ctx) <= 0:
        return _MergeInputs(stop=lambda: _timeout(n, ctx))
    comparison_current = True
    if enabled(ctx, base):
        comparison_current = comparison is not None and comparison.current(n, ctx, git, pr, head, target_sha, status)
        if comparison_current and comparison.infrastructure:
            # The unlocked comparison already handled this infrastructure state.
            status = "pass"
    return _MergeInputs(base_sha=target_sha, paths=paths, decision=decision, status=status,
                        batch_branch=batch_branch, comparison_current=comparison_current)


def _batch_pr(parent: int, ctx: RunContext) -> None:
    children = ctx.tracker.sub_issues(parent)
    if not children or any(child.state != "closed" for child in children):
        return
    branch = f"foragent/batch-{parent}"
    if any(pr.head == branch for pr in ctx.tracker.list_prs("merged", branch)):
        return
    ctx.git.fetch("origin", ctx.config.base_branch)
    ctx.git.fetch("origin", branch)
    base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
    head = ctx.git.ref_sha(f"origin/{branch}")
    with tempfile.TemporaryDirectory(prefix=f"foragent-batch-{parent}-", dir=ctx.store.root) as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            ctx.git.at(worktree).check_publication(base)
        finally:
            ctx.git.remove_worktree(worktree)
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    lines = [f"Closes #{parent}", "", "## Children", ""]
    for child in sorted(children, key=lambda child: child.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        lines.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    title = f"Batch: {ctx.tracker.get_issue(parent).title} (#{parent})"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        board.sync_status(ctx, parent, "PR open")
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    board.sync_status(ctx, parent, "PR open")
    ctx.tracker.comment(parent, f"PR #{pr} is waiting for your review\n\nhttps://github.com/{ctx.repo}/pull/{pr}")


def retitle_for_merge(
    ctx: RunContext, pr: int, *, title: str, current_title: str | None, base: str,
    body: Callable[[], str], note: Callable[[str], None],
) -> dict[str, str]:
    """Give the PR its issue-derived title before merging; return extra merge_pr arguments.

    Branch commit messages (WIP checkpoints) never decide the title. The squash
    subject is the PR title, so it is passed only when retitling failed (other
    than a rate limit): the merge then still uses the issue-derived subject.
    """
    if title != current_title:
        try:
            ctx.tracker.update_pr(pr, base=base, title=title, body=body())
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            note(f"PR #{pr} retitle failed; merging with subject {title!r}: {exc}")
            return {"subject": title}
    return {}


def _check_publication(git: GitRepo, target_branch: str, base_sha: str | None = None) -> None:
    if base_sha is None:
        base_sha = git.ref_sha(f"origin/{target_branch}")
    try:
        git.check_publication(base_sha)
    except PublicationError as exc:
        raise AttemptFailure(str(exc), target_branch) from exc


def _preserve_failed_implementation(
    n: int, ctx: FlagContext, git: GitRepo, reason: str, *, approval: Approval | None,
    approved_ref: str = "HEAD", title: str | None = None, pushed_head: str | None = None,
    tracker_available: bool = True, push_branch: Callable[[], None] | None = None,
) -> None:
    """Save stopped work before cleanup, including after the runner has died."""
    branch = f"foragent/issue-{n}"
    run = ctx.store.get_run(n)
    git.local_skill_paths.update(run.local_skill_paths if run is not None else ())
    conflicts = sorted(set(git.conflict_paths()) | set(run.merge_conflicts if run is not None else ()))
    if conflicts:
        ctx.store.update_run(n, merge_conflicts=conflicts)
        if git.merge_in_progress():
            # Save the exact partial resolution without declaring its target merged.
            git.quit_merge()
    git.stage_changes()
    if approval is not None:
        git.restore_staged_paths(approved_ref, approval.acceptance_blobs)
    if git.has_staged_changes():
        git.commit(f"WIP: {title or 'Issue'} (#{n}) [{reason}]")
    if git.head_sha() == pushed_head or not tracker_available:
        return
    try:
        pr = ctx.tracker.find_pr(branch)
    except TrackerError as exc:
        _append(ctx.store.log_path(n), f"WIP kept on local branch {branch}; open PR lookup failed: {exc}")
        return
    # An unverified WIP must not replace the head seen by an open PR's CI/review.
    if pr is None:
        if push_branch is not None:
            push_branch()
        else:
            git.push(branch)


def _push_branch(attempt: Implementation) -> None:
    if attempt.rebuilt:
        attempt.git.push(attempt.branch, lease=attempt.rebuild_lease)
        attempt.rebuild_lease = attempt.git.head_sha()
    else:
        attempt.git.push(attempt.branch)
    attempt.pushed_head = attempt.git.head_sha()


def _skills_line(run: Run) -> str:
    """The implementer's own `Skills used:` report; every implementer is asked for one."""
    return report_line(run.skills_used, run.skills_reported)


def _render_pr_body(attempt: Implementation, try_text: str | None = None) -> str:
    n, ctx = attempt.n, attempt.ctx
    lane = ctx.store.get_run(n).lane
    globs = ctx.config.lanes.get(lane, {}).get("paths", []) if lane != "main" else None
    inside = set(touched(attempt.paths, globs)) if globs is not None else set(attempt.paths)
    outside = [path for path in attempt.paths if path not in inside]
    return _pr_body(
        n, attempt.parsed, attempt.checks, ctx.store.get_run(n).attempts, attempt.reviews, try_text, outside_lane=outside,
        fallback_model=ctx.store.get_run(n).fallback_model,
        attempt_tiers=ctx.store.get_run(n).attempt_tiers,
        missing_skills=attempt.missing_skills,
        skills_line=_skills_line(ctx.store.get_run(n)),
        rebuttals="\n\n".join(attempt.rebuttals),
        review_scope=ctx.config.review_scope, acceptance_dir=ctx.config.acceptance_dir,
    )


def _open_or_update_pr(attempt: Implementation, title: str, body: str, base_sha: str | None = None) -> int:
    _check_publication(attempt.git, attempt.target_branch, base_sha)
    pr = attempt.ctx.tracker.find_pr(attempt.branch)
    if pr is None:
        pr = attempt.ctx.tracker.create_pr(head=attempt.branch, base=attempt.target_branch, title=title, body=body)
        attempt.pr_title = title
    else:
        _update_open_pr(attempt, pr, title, body)
    board.sync_status(attempt.ctx, attempt.n, "PR open")
    return pr


def _update_open_pr(attempt: Implementation, pr: int, title: str, body: str) -> None:
    """Retarget and refresh an open PR; a failed retitle alone never stops the run.

    The merge retitles again and, if that still fails, squashes with the
    issue-derived subject (retitle_for_merge). A base that was not retargeted
    is caught by the final merge inputs, which compare the live PR base.
    """
    ctx = attempt.ctx
    try:
        ctx.tracker.update_pr(pr, base=attempt.target_branch, title=title, body=body)
    except TrackerError as exc:
        if exc.is_rate_limit:
            raise
        attempt.pr_title = None  # unknown: the merge retitles before squashing
        _append(attempt.log, f"PR #{pr} retitle failed; retitling again at merge: {exc}")
        try:
            # Keep the current title so a rejected title alone does not block the retarget.
            current = next((item for item in ctx.tracker.list_prs("open", attempt.branch)
                            if item.number == pr), None)
            if current is not None and current.title != title:
                ctx.tracker.update_pr(pr, base=attempt.target_branch, title=current.title, body=body)
                attempt.pr_title = current.title
        except TrackerError as retry:
            if retry.is_rate_limit:
                raise
            _append(attempt.log, f"PR #{pr} refresh under its current title failed: {retry}")
    else:
        attempt.pr_title = title


def _publish(attempt: Implementation) -> str:
    return _publish_locked(attempt)


def _publish_locked(attempt: Implementation) -> str:
    from foragent.run_checks import _verify_implementation
    from foragent.run_conflicts import _block_rebuild, _merge_moved_base, _resolve_conflicts
    from foragent import runner

    # Only metadata is read live; instructions and checks always use the snapshot.
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree, log = attempt.worktree, attempt.log
    issue = ctx.tracker.get_issue(n)
    title = f"{issue.title} (#{n})"
    stage_exclusions(attempt)
    if git.has_staged_changes():
        git.commit(title)
    _progress(n, ctx)
    verified_head_sha = git.head_sha()
    base_merges = 0

    def finish_base() -> None:
        nonlocal verified_head_sha
        if runner._remaining(n, ctx) <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        stage_exclusions(attempt)
        if git.has_staged_changes():
            git.commit(title)
        verified_head_sha = git.head_sha()

    def refresh_base(move: BaseMove, conflicts: list[str] | None = None) -> str:
        nonlocal base_merges
        neutral = conflicts is None and base_move_is_neutral(attempt, move)
        if not neutral and base_merges == 3:
            flag(ctx, n, Reason.TIMEOUT, "base kept moving")
            raise ExecutorStopped("timeout")
        _append(log, f"{REUSED if neutral else REVERIFY}: {move.target_sha}")
        # Exclude the merged base's paths from policy and patch replay. The
        # separate verified_base_sha advances only after verification/push.
        attempt.start_ref = move.target_sha
        if not neutral:
            # Protect what passes on the new base before merging it (#483).
            from foragent.run_implementation import follow_base
            follow_base(attempt)
        if conflicts is None:
            conflicts = _merge_moved_base(attempt, move.target_sha)
        if conflicts:
            if base_merges == 3:
                flag(ctx, n, Reason.TIMEOUT, "base kept moving")
                raise ExecutorStopped("timeout")
            _resolve_conflicts(attempt, conflicts)
            neutral = False
        elif not neutral:
            check_failure = _verify_implementation(attempt)
            if check_failure:
                raise AttemptFailure(check_failure)
        if not neutral:
            base_merges += 1
        finish_base()
        return move.target_sha

    target_sha = None
    if not attempt.review_hold:
        move = fetch_move(attempt, verified_head_sha)
        # Publication can fetch a newer ref in another lane. Evidence belongs
        # to this captured base, including when it was already in the head.
        verified_base_sha = move.target_sha
        if not git.is_ancestor(move.target_sha, move.head_sha):
            target_sha = refresh_base(move)
    if not attempt.review_hold and runner._remaining(n, ctx) <= 0:
        return _timeout(n, ctx)
    _check_publication(git, attempt.target_branch, None if attempt.review_hold else verified_base_sha)
    _push_branch(attempt)
    _progress(n, ctx)
    _append(log, f"Pushed verified HEAD: {verified_head_sha}")
    try_text = None
    try_path = worktree / ".foragent/TRY.md"
    if not attempt.review_hold and try_path.is_file():
        try_text = try_path.read_bytes().decode("utf-8", errors="replace")
        saved_try = ctx.store.root / "try" / f"{n}.md"
        saved_try.parent.mkdir(parents=True, exist_ok=True)
        saved_try.write_text(try_text, encoding="utf-8")

    def pr_body() -> str:
        return _render_pr_body(attempt, try_text)

    pr = _open_or_update_pr(attempt, title, pr_body(), None if attempt.review_hold else verified_base_sha)
    if target_sha is not None:
        ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
    _progress(n, ctx)
    pr_ready_at = ctx.now().timestamp()
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
        return _hold_changed_head(n, ctx, link)
    if attempt.review_hold:
        summary = (
            "Reviewer repeated a disputed finding after implementer rebuttals"
            if attempt.review_hold_reason == Reason.REVIEW_DISPUTE
            else f"Reviewer still found defects after {attempt.review_failures} rounds"
        )
        details = f"{attempt.failure}\n\n{link}"
        flag(ctx, n, attempt.review_hold_reason, summary, details)
        return "held"

    def policy_hold(decision: Decision, batch_branch: str | None = attempt.batch_branch) -> str | None:
        if decision.kind == "hold":
            flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if not _policy_allows(decision, attempt.target_branch, batch_branch):
            ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
            ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
            ctx.tracker.remove_labels(n, [RUNNING])
            return "pr-open"

    if stopped := policy_hold(merge_decision(
        changed_paths=ctx.tracker.pr_changed_files(pr),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
    )):
        return stopped

    def publish_refresh(target_sha: str) -> str | None:
        nonlocal pr_ready_at, ci_wait, verified_base_sha
        _check_publication(git, attempt.target_branch, target_sha)
        _push_branch(attempt)
        _progress(n, ctx)
        _append(log, f"Pushed verified HEAD: {verified_head_sha}")
        _update_open_pr(attempt, pr, title, pr_body())
        ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
        pr_ready_at = ctx.now().timestamp()
        ci_wait = _CIWait(pr_ready_at)
        verified_base_sha = target_sha
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return _hold_changed_head(n, ctx, link)
        decision = merge_decision(
            changed_paths=git.changed_paths(target_sha),
            issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
        )
        return policy_hold(decision)

    def ci_failed() -> None:
        if attempt.rebuilt:
            logs = ctx.tracker.pr_failed_logs(pr)
            _hold_if_not_started(n, ctx, pr, logs)
            raise ExecutorStopped(_block_rebuild(attempt, f"CI failed:\n{logs}"))
        _ci_failure(n, ctx, pr, verified_head_sha, attempt.target_branch)

    ci_wait = _CIWait(pr_ready_at)
    queued_status = None
    # Result-affecting base merges are bounded by refresh_base's budget and
    # result-neutral ones by the run's time budget, which refresh_base checks.
    while True:
        _phase(n, ctx, "ci")
        if stopped := _wait_for_ci(n, ctx, git, pr, verified_head_sha, pr_ready_at,
                                  ci_wait, ci_failed, queued_status,
                                  comparison_mode=enabled(ctx, attempt.target_branch)):
            return stopped
        queued_status = None

        # Refresh a base that already moved before joining the merge queue.
        # The final snapshot still detects changes during lock acquisition.
        ctx.git.fetch("origin", attempt.target_branch)
        target_sha = git.ref_sha(f"origin/{attempt.target_branch}")
        if target_sha != verified_base_sha:
            target_sha = refresh_base(capture_move(git, target_sha))
            if stopped := publish_refresh(target_sha):
                return stopped
            continue

        comparison = None
        if enabled(ctx, attempt.target_branch):
            comparison = require_comparison(attempt, pr, verified_head_sha, ci_wait.infrastructure,
                                            base_sha=verified_base_sha, status=ci_wait.status,
                                            deadline=ci_wait.deadline)
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return _hold_changed_head(n, ctx, link)

        with _merge_lock(n, ctx):
            inputs = _final_merge_inputs(
                n, ctx, git, pr, attempt.branch, attempt.target_branch,
                verified_head_sha, attempt.approval, pr_ready_at, comparison=comparison,
            )
            move = None
            continue_merge = (
                inputs.stop is None
                and _policy_allows(inputs.decision, attempt.target_branch, inputs.batch_branch)
                and inputs.status in {"pass", "none"}
            )
            if continue_merge and inputs.base_sha != verified_base_sha:
                move = capture_move(git, inputs.base_sha)
            if continue_merge and move is None and inputs.comparison_current:
                _check_publication(git, attempt.target_branch, inputs.base_sha)
                _phase(n, ctx, "merging")
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = retitle_for_merge(
                    ctx, pr, title=title, current_title=attempt.pr_title,
                    base=attempt.target_branch, body=pr_body, note=lambda line: _append(log, line),
                )
                if not subject:
                    attempt.pr_title = title
                try:
                    ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha, **subject)
                except TrackerError as exc:
                    if exc.is_rate_limit:
                        raise
                    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                        inputs.stop = lambda: _hold_changed_head(n, ctx, link)
                    else:
                        raise
                else:
                    ctx.store.record_merge(n, ctx.now())
                    if inputs.decision.kind == "merge":
                        runner.after_merge(ctx, pr, attempt.target_branch)
                    _progress(n, ctx)
                    try:
                        if attempt.target_branch == attempt.batch_branch:
                            ctx.tracker.close_issue(n, f"Merged into {attempt.batch_branch} (PR #{pr})")
                        else:
                            ctx.tracker.close_issue(n)
                    except TrackerError as exc:
                        if exc.is_rate_limit:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [READY, RUNNING])
                    board.sync_status(ctx, n, "Done")
                    if attempt.target_branch == attempt.batch_branch and attempt.parent is not None:
                        try:
                            _batch_pr(attempt.parent, ctx)
                        except (GitError, TrackerError) as exc:
                            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                                raise
                            _append(log, f"Batch PR finalization deferred to tick: {exc}")
                    _progress(n, ctx)
                    return "merged"
        if inputs.stop is not None:
            return inputs.stop()
        if stopped := policy_hold(inputs.decision, inputs.batch_branch):
            return stopped
        if not continue_merge or (move is None and not inputs.comparison_current):
            queued_status = inputs.status
            continue
        if move is not None:
            # Pin the observed base; all merging/rechecks/pushes run unlocked.
            target_sha = refresh_base(move)
            if stopped := publish_refresh(target_sha):
                return stopped
            continue


def _publish_reviewed_head(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, base_ref: str, base_sha: str,
    approval: Approval, approved: Callable[[], bool], batch_branch: str | None, parent: int | None,
    *, comparison_attempt=None,
) -> str:
    from foragent import runner

    worktree = git.root
    log = ctx.store.log_path(n)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    # A pass applies only to this head and these instructions. Neither the
    # reviewer nor an orchestrator verdict can waive the remaining checks.
    def unchanged() -> str | None:
        if not approved():
            return "hash-mismatch"
        if ctx.tracker.pr_head_sha(pr.number) != head:
            return _hold_changed_head(n, ctx, link)
        current = next((item for item in ctx.tracker.list_prs("open", pr.head)
                        if item.number == pr.number), None)
        ctx.git.fetch("origin", pr.base)
        if current is None or current.base != pr.base or ctx.git.ref_sha(base_ref) != base_sha:
            flag(ctx, n, Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
            return "base-changed"
        if not _acceptance_matches(n, ctx, git, approval):
            return "hash-mismatch"
        return None

    if stopped := unchanged():
        return stopped

    def policy_hold(decision: Decision, current_batch: str | None = batch_branch) -> str | None:
        if decision.kind == "hold":
            flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if not _policy_allows(decision, pr.base, current_batch):
            flag(ctx, n, Reason.RISK_HIGH, "PR is waiting for your review.", link)
            return "pr-open"
        return None

    if stopped := policy_hold(merge_decision(
        changed_paths=sorted(set(git.changed_paths(base_sha)) | set(ctx.tracker.pr_changed_files(pr.number))),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
    )):
        return stopped
    started = ctx.now().timestamp()
    ci_wait = _CIWait(started)
    queued_status = None
    while True:
        if stopped := _wait_for_ci(
            n, ctx, git, pr.number, head, started, ci_wait,
            lambda: _ci_failure(n, ctx, pr.number, head, pr.base), queued_status,
            comparison_mode=comparison_attempt is not None,
        ):
            return stopped
        comparison = None
        if comparison_attempt is not None:
            comparison = require_comparison(comparison_attempt, pr.number, head, ci_wait.infrastructure,
                                            base_sha=base_sha, status=ci_wait.status,
                                            deadline=ci_wait.deadline)
            if stopped := unchanged():
                return stopped
        with _merge_lock(n, ctx):
            inputs = _final_merge_inputs(
                n, ctx, git, pr.number, pr.head, pr.base, head, approval, started, base_sha=base_sha,
                approved_body_sha=approval.body_sha,
                comparison=comparison,
            )
            continue_merge = (inputs.stop is None and _policy_allows(inputs.decision, pr.base, inputs.batch_branch)
                              and inputs.status in {"pass", "none"} and inputs.comparison_current)
            if continue_merge:
                _check_publication(git, pr.base, inputs.base_sha)
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = retitle_for_merge(
                    ctx, pr.number, title=title, current_title=pr.title, base=pr.base,
                    body=lambda: pr.body, note=lambda line: _append(log, line),
                )
                pr = replace(pr, title=title)
                try:
                    ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head, **subject)
                except TrackerError as exc:
                    if exc.is_rate_limit:
                        raise
                    if ctx.tracker.pr_head_sha(pr.number) != head:
                        inputs.stop = lambda: _hold_changed_head(n, ctx, link)
                    else:
                        raise
                else:
                    ctx.store.record_merge(n, ctx.now())
                    if review_mode_for(inputs.paths, ctx.config) == "fast":
                        runner.after_merge(ctx, pr.number, pr.base)
                    try:
                        if pr.base == batch_branch:
                            ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr.number})")
                        else:
                            ctx.tracker.close_issue(n)
                    except TrackerError as exc:
                        if exc.is_rate_limit:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [READY, RUNNING, BLOCKED])
                    if pr.base == batch_branch and parent is not None:
                        try:
                            _batch_pr(parent, ctx)
                        except (GitError, TrackerError) as exc:
                            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                                raise
                            _append(log, f"Batch PR finalization deferred to tick: {exc}")
                    return "merged"
        if inputs.stop is not None:
            return inputs.stop()
        if stopped := policy_hold(inputs.decision, inputs.batch_branch):
            return stopped
        queued_status = inputs.status

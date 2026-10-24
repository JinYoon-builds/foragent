"""Verified PR contents, publication, CI and merge policy."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from foragent import board
from foragent.actions import FlagContext, flag
from foragent.git import GitError, GitRepo, PublicationError
from foragent.issue import ParsedIssue
from foragent.labels import BLOCKED, READY, RUNNING
from foragent.policy import merge_decision, review_mode_for, touched
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped
from foragent.run_base_move import REUSED, REVERIFY, base_move_is_neutral, capture_move
from foragent.run_checks import _acceptance_matches, _append
from foragent.run_lane_skills import _inject_lane_skills
from foragent.run_prompts import review_scope_rules
from foragent.run_waits import _credited_wait, _phase, _progress, _timeout
from foragent.state import Approval
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


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    failure = CIFailure(ctx.tracker.pr_failed_logs(pr), target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise ExecutorStopped(_hold_changed_head(n, ctx, link))
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
    with ExitStack() as stack:
        _credited_wait(n, ctx, lambda: stack.enter_context(ctx.store.merge_lock()))
        yield


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
    title = f"Batch: {ctx.tracker.get_issue(parent).title}"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        board.sync_status(ctx, parent, "PR open")
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    board.sync_status(ctx, parent, "PR open")
    ctx.tracker.comment(parent, f"PR #{pr} is waiting for your review\n\nhttps://github.com/{ctx.repo}/pull/{pr}")


def _check_publication(git: GitRepo, target_branch: str) -> None:
    try:
        git.check_publication(f"origin/{target_branch}")
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
        rebuttals="\n\n".join(attempt.rebuttals),
        review_scope=ctx.config.review_scope, acceptance_dir=ctx.config.acceptance_dir,
    )


def _open_or_update_pr(attempt: Implementation, title: str, body: str) -> int:
    _check_publication(attempt.git, attempt.target_branch)
    pr = attempt.ctx.tracker.find_pr(attempt.branch)
    if pr is None:
        pr = attempt.ctx.tracker.create_pr(head=attempt.branch, base=attempt.target_branch, title=title, body=body)
    else:
        attempt.ctx.tracker.update_pr(pr, base=attempt.target_branch, title=title, body=body)
    board.sync_status(attempt.ctx, attempt.n, "PR open")
    return pr


def _publish(attempt: Implementation) -> str:
    with _merge_lock(attempt.n, attempt.ctx):
        return _publish_locked(attempt)


def _publish_locked(attempt: Implementation) -> str:
    from foragent.run_checks import _verify_implementation
    from foragent.run_conflicts import _block_rebuild, _resolve_conflicts
    from foragent import runner

    # Only metadata is read live; instructions and checks always use the snapshot.
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree, log = attempt.worktree, attempt.log
    issue = ctx.tracker.get_issue(n)
    title = f"{issue.title} (#{n})"
    if git.has_staged_changes():
        git.commit(title)
    _progress(n, ctx)
    verified_head_sha = git.head_sha()
    base_merges = 0

    def refresh_base() -> str | None:
        nonlocal verified_head_sha, base_merges
        ctx.git.fetch("origin", attempt.target_branch)
        target_ref = f"origin/{attempt.target_branch}"
        target_sha = git.ref_sha(target_ref)
        if git.is_ancestor(target_sha, verified_head_sha):
            return None
        # Docs and other issues' pending locked tests cannot change this
        # branch's verified result, so they neither re-verify nor use the budget.
        # The shared remote-tracking ref can move again (another fetch) before
        # the merge, so pin the range once, classify it, and merge exactly it.
        move = capture_move(git, target_sha)
        target_sha = move.target_sha
        neutral = base_move_is_neutral(attempt, move)
        if not neutral and base_merges == 3:
            flag(ctx, n, Reason.TIMEOUT, "base kept moving")
            raise ExecutorStopped("timeout")
        _append(log, f"{REUSED if neutral else REVERIFY}: {target_sha}")
        _inject_lane_skills(ctx, n, git, target_sha)
        try:
            git.merge_base(target_sha)
        except GitError:
            conflicts = git.conflict_paths()
            if not conflicts:
                raise
            _resolve_conflicts(attempt, conflicts)
            neutral = False
        else:
            if not neutral:
                check_failure = _verify_implementation(attempt)
                if check_failure:
                    raise AttemptFailure(check_failure)
        if not neutral:
            base_merges += 1
        if runner._remaining(n, ctx) <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        if git.has_staged_changes():
            git.commit(title)
        verified_head_sha = git.head_sha()
        return target_sha

    target_sha = refresh_base() if not attempt.review_hold else None
    if not attempt.review_hold and runner._remaining(n, ctx) <= 0:
        return _timeout(n, ctx)
    _check_publication(git, attempt.target_branch)
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

    pr = _open_or_update_pr(attempt, title, pr_body())
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
    decision = merge_decision(
        changed_paths=ctx.tracker.pr_changed_files(pr),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
    )
    if decision.kind == "hold":
        flag(ctx, n, decision.reason, decision.summary, link)
        return "held"
    if decision.kind == "hold_mode" and (decision.mode == "careful" or not attempt.batch_branch):
        ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
        ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
        ctx.tracker.remove_labels(n, [RUNNING])
        return "pr-open"

    # Result-affecting base merges are bounded by refresh_base's budget and
    # result-neutral ones by the run's time budget, which refresh_base checks.
    while True:
        _phase(n, ctx, "ci")
        has_workflows = any(
            path.is_file()
            for pattern in ("*.yml", "*.yaml")
            for path in (worktree / ".github/workflows").glob(pattern)
        )
        deadline = ctx.now().timestamp() + 20 * 60
        for poll in range(41):
            status = ctx.tracker.pr_checks(pr)
            _progress(n, ctx)
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return _hold_changed_head(n, ctx, link)
            # Actions checks can register after each push, including base merges.
            if status == "none" and has_workflows and ctx.now().timestamp() - pr_ready_at < 180:
                status = "pending"
            if status in {"pass", "none"}:
                break
            if status == "fail":
                if attempt.rebuilt:
                    raise ExecutorStopped(_block_rebuild(attempt, f"CI failed:\n{ctx.tracker.pr_failed_logs(pr)}"))
                _ci_failure(n, ctx, pr, verified_head_sha, attempt.target_branch)
            remaining = deadline - ctx.now().timestamp()
            if remaining <= 0 or poll == 40:
                flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                return "timeout"
            ctx.sleep(min(30, remaining))

        target_sha = refresh_base()
        target_ref = f"origin/{attempt.target_branch}"
        if target_sha is not None:
            _check_publication(git, attempt.target_branch)
            _push_branch(attempt)
            _progress(n, ctx)
            _append(log, f"Pushed verified HEAD: {verified_head_sha}")
            ctx.tracker.update_pr(pr, base=attempt.target_branch, title=title, body=pr_body())
            ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
            pr_ready_at = ctx.now().timestamp()
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return _hold_changed_head(n, ctx, link)
            decision = merge_decision(
                changed_paths=git.changed_paths(target_ref),
                issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
            )
            if decision.kind == "hold":
                flag(ctx, n, decision.reason, decision.summary, link)
                return "held"
            if decision.kind == "hold_mode" and (decision.mode == "careful" or not attempt.batch_branch):
                ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
                ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
                ctx.tracker.remove_labels(n, [RUNNING])
                return "pr-open"
            continue
        _check_publication(git, attempt.target_branch)
        _phase(n, ctx, "merging")
        try:
            ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return _hold_changed_head(n, ctx, link)
            raise
        if decision.kind == "merge":
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


def _publish_reviewed_head(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, base_ref: str, base_sha: str,
    approval: Approval, approved: Callable[[], bool], batch_branch: str | None, parent: int | None,
) -> str:
    from foragent import runner

    worktree = git.root
    log = ctx.store.log_path(n)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    with _merge_lock(n, ctx):
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
        paths = sorted(set(git.changed_paths(base_ref)) | set(ctx.tracker.pr_changed_files(pr.number)))

        def policy_hold() -> str | None:
            decision = merge_decision(changed_paths=paths, issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config)
            if decision.kind == "hold":
                flag(ctx, n, decision.reason, decision.summary, link)
                return "held"
            if decision.kind == "hold_mode" and (decision.mode == "careful" or pr.base != batch_branch):
                flag(ctx, n, Reason.RISK_HIGH, "PR is waiting for your review.", link)
                return "pr-open"
            return None

        if stopped := policy_hold():
            return stopped
        has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                            for path in (worktree / ".github/workflows").glob(pattern))
        started = ctx.now().timestamp()
        for poll in range(41):
            if runner._remaining(n, ctx) <= 0:
                return _timeout(n, ctx)
            status = ctx.tracker.pr_checks(pr.number)
            _progress(n, ctx)
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            if status == "none" and has_workflows and ctx.now().timestamp() - started < 180:
                status = "pending"
            if status in {"pass", "none"}:
                break
            if status == "fail":
                _ci_failure(n, ctx, pr.number, head, pr.base)
            if ctx.now().timestamp() - started >= 1200 or poll == 40:
                flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                return "timeout"
            ctx.sleep(min(30, runner._remaining(n, ctx)))
        if stopped := unchanged():
            return stopped
        if stopped := policy_hold():
            return stopped
        if runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        _check_publication(git, pr.base)
        try:
            ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            raise
        if review_mode_for(paths, ctx.config) == "fast":
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

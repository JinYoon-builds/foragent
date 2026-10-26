"""Publish a verified implementation: base refresh, re-verify, CI and merge."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.policy import Decision
    from foragent.run_attempt import Implementation
    from foragent.run_base_move import BaseMove


def _publish(attempt: Implementation) -> str:
    run_publish.wait_for_overlap(attempt)
    return run_publish._publish_locked(attempt)


def _publish_locked(attempt: Implementation) -> str:
    from foragent.run_checks import _verify_implementation
    from foragent.run_conflicts import _block_rebuild, _merge_moved_base, _resolve_conflicts
    from foragent import runner

    # Only metadata is read live; instructions and checks always use the snapshot.
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree, log = attempt.worktree, attempt.log
    issue = ctx.tracker.get_issue(n)
    title = f"{issue.title} (#{n})"
    run_publish.commit_exclusions(attempt, title)
    run_publish._progress(n, ctx)
    verified_head_sha = git.head_sha()
    base_merges = 0

    def finish_base() -> None:
        nonlocal verified_head_sha
        if runner._remaining(n, ctx) <= 0:
            raise run_publish.ExecutorStopped(run_publish._timeout(n, ctx))
        run_publish.commit_exclusions(attempt, title)
        verified_head_sha = git.head_sha()

    def refresh_base(move: BaseMove, conflicts: list[str] | None = None) -> str:
        nonlocal base_merges
        neutral = conflicts is None and run_publish.base_move_is_neutral(attempt, move)
        if not neutral and base_merges == 3:
            run_publish.flag(ctx, n, run_publish.Reason.TIMEOUT, "base kept moving")
            raise run_publish.ExecutorStopped("timeout")
        run_publish._append(log, f"{run_publish.REUSED if neutral else run_publish.REVERIFY}: {move.target_sha}")
        # Exclude the merged base's paths from policy and patch replay. The
        # separate verified_base_sha advances only after verification/push.
        attempt.start_ref = move.target_sha
        if conflicts is None:
            conflicts = _merge_moved_base(attempt, move.target_sha)
        if conflicts:
            if base_merges == 3:
                run_publish.flag(ctx, n, run_publish.Reason.TIMEOUT, "base kept moving")
                raise run_publish.ExecutorStopped("timeout")
            _resolve_conflicts(attempt, conflicts)
            neutral = False
        elif not neutral:
            check_failure = _verify_implementation(attempt)
            if check_failure:
                raise run_publish.AttemptFailure(check_failure)
        if not neutral:
            base_merges += 1
        finish_base()
        return move.target_sha

    target_sha = None
    if not attempt.review_hold:
        move = run_publish.fetch_move(attempt, verified_head_sha)
        # Publication can fetch a newer ref in another lane. Evidence belongs
        # to this captured base, including when it was already in the head.
        verified_base_sha = move.target_sha
        if not git.is_ancestor(move.target_sha, move.head_sha):
            target_sha = refresh_base(move)
    if not attempt.review_hold and runner._remaining(n, ctx) <= 0:
        return run_publish._timeout(n, ctx)
    run_publish._check_publication(git, attempt.target_branch, None if attempt.review_hold else verified_base_sha)
    run_publish._push_branch(attempt)
    run_publish._progress(n, ctx)
    run_publish._append(log, f"Pushed verified HEAD: {verified_head_sha}")
    try_text = None
    try_path = worktree / ".foragent/TRY.md"
    if not attempt.review_hold and try_path.is_file():
        try_text = try_path.read_bytes().decode("utf-8", errors="replace")
        saved_try = ctx.store.root / "try" / f"{n}.md"
        saved_try.parent.mkdir(parents=True, exist_ok=True)
        saved_try.write_text(try_text, encoding="utf-8")

    def pr_body() -> str:
        return run_publish._render_pr_body(attempt, try_text)

    pr = run_publish._open_or_update_pr(attempt, title, pr_body(), None if attempt.review_hold else verified_base_sha)
    if target_sha is not None:
        ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
    run_publish._progress(n, ctx)
    pr_ready_at = ctx.now().timestamp()
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
        return run_publish._hold_changed_head(n, ctx, link)
    if attempt.review_hold:
        summary = (
            "Reviewer repeated a disputed finding after implementer rebuttals"
            if attempt.review_hold_reason == run_publish.Reason.REVIEW_DISPUTE
            else f"Reviewer still found defects after {attempt.review_failures} rounds"
        )
        details = f"{attempt.failure}\n\n{link}"
        run_publish.flag(ctx, n, attempt.review_hold_reason, summary, details)
        return "held"

    def policy_hold(decision: Decision, batch_branch: str | None = attempt.batch_branch) -> str | None:
        if decision.kind == "hold":
            run_publish.flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if not run_publish._policy_allows(decision, attempt.target_branch, batch_branch):
            ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
            ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
            ctx.tracker.remove_labels(n, [run_publish.RUNNING])
            return "pr-open"

    if stopped := policy_hold(run_publish.merge_decision(
        changed_paths=ctx.tracker.pr_changed_files(pr),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
        risk_always_human=run_publish._risk_always_human(ctx),
    )):
        return stopped

    def publish_refresh(target_sha: str) -> str | None:
        nonlocal pr_ready_at, ci_wait, verified_base_sha
        run_publish._check_publication(git, attempt.target_branch, target_sha)
        run_publish._push_branch(attempt)
        run_publish._progress(n, ctx)
        run_publish._append(log, f"Pushed verified HEAD: {verified_head_sha}")
        run_publish._update_open_pr(attempt, pr, title, pr_body())
        ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
        pr_ready_at = ctx.now().timestamp()
        ci_wait = run_publish._CIWait(pr_ready_at)
        verified_base_sha = target_sha
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return run_publish._hold_changed_head(n, ctx, link)
        decision = run_publish.merge_decision(
            changed_paths=git.changed_paths(target_sha),
            issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
            risk_always_human=run_publish._risk_always_human(ctx),
        )
        return policy_hold(decision)

    def ci_failed() -> None:
        if attempt.rebuilt:
            logs = ctx.tracker.pr_failed_logs(pr)
            run_publish._hold_if_not_started(n, ctx, pr, logs)
            raise run_publish.ExecutorStopped(_block_rebuild(attempt, f"CI failed:\n{logs}"))
        run_publish._ci_failure(n, ctx, pr, verified_head_sha, attempt.target_branch)

    ci_wait = run_publish._CIWait(pr_ready_at)
    queued_status = None
    # Result-affecting base merges are bounded by refresh_base's budget and
    # result-neutral ones by the run's time budget, which refresh_base checks.
    while True:
        run_publish._phase(n, ctx, "ci")
        if stopped := run_publish._wait_for_ci(n, ctx, git, pr, verified_head_sha, pr_ready_at,
                                              ci_wait, ci_failed, queued_status,
                                              comparison_mode=run_publish.enabled(ctx, attempt.target_branch)):
            return stopped
        queued_status = None

        # Refresh a base that already moved before joining the merge queue.
        # The final snapshot still detects changes during lock acquisition.
        ctx.git.fetch("origin", attempt.target_branch)
        target_sha = git.ref_sha(f"origin/{attempt.target_branch}")
        if target_sha != verified_base_sha:
            target_sha = refresh_base(run_publish.capture_move(git, target_sha))
            if stopped := publish_refresh(target_sha):
                return stopped
            continue

        comparison = None
        if run_publish.enabled(ctx, attempt.target_branch):
            comparison = run_publish.require_comparison(attempt, pr, verified_head_sha, ci_wait.infrastructure,
                                                        base_sha=verified_base_sha, status=ci_wait.status,
                                                        deadline=ci_wait.deadline)
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return run_publish._hold_changed_head(n, ctx, link)

        run_publish.wait_for_overlap(attempt)
        with run_publish._merge_lock(n, ctx):
            inputs = run_publish._final_merge_inputs(
                n, ctx, git, pr, attempt.branch, attempt.target_branch,
                verified_head_sha, attempt.approval, pr_ready_at, comparison=comparison,
            )
            move = None
            continue_merge = (
                inputs.stop is None
                and run_publish._policy_allows(inputs.decision, attempt.target_branch, inputs.batch_branch)
                and inputs.status in {"pass", "none"}
            )
            if continue_merge and inputs.base_sha != verified_base_sha:
                move = run_publish.capture_move(git, inputs.base_sha)
            if continue_merge and move is None and inputs.comparison_current:
                run_publish._check_publication(git, attempt.target_branch, inputs.base_sha)
                run_publish._phase(n, ctx, "merging")
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = run_publish.retitle_for_merge(
                    ctx, pr, title=title, current_title=attempt.pr_title,
                    base=attempt.target_branch, body=pr_body, note=lambda line: run_publish._append(log, line),
                )
                if not subject:
                    attempt.pr_title = title
                try:
                    ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha, **subject)
                except run_publish.TrackerError as exc:
                    if exc.is_rate_limit:
                        raise
                    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                        inputs.stop = lambda: run_publish._hold_changed_head(n, ctx, link)
                    else:
                        raise
                else:
                    ctx.store.record_merge(n, ctx.now())
                    if inputs.decision.kind == "merge":
                        runner.after_merge(ctx, pr, attempt.target_branch)
                    run_publish._progress(n, ctx)
                    try:
                        if attempt.target_branch == attempt.batch_branch:
                            ctx.tracker.close_issue(n, f"Merged into {attempt.batch_branch} (PR #{pr})")
                        else:
                            ctx.tracker.close_issue(n)
                    except run_publish.TrackerError as exc:
                        if exc.is_rate_limit:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [run_publish.READY, run_publish.RUNNING])
                    run_publish.board.sync_status(ctx, n, "Done")
                    if attempt.target_branch == attempt.batch_branch and attempt.parent is not None:
                        try:
                            run_publish._batch_pr(attempt.parent, ctx)
                        except (run_publish.GitError, run_publish.TrackerError) as exc:
                            if isinstance(exc, run_publish.TrackerError) and exc.is_rate_limit:
                                raise
                            run_publish._append(log, f"Batch PR finalization deferred to tick: {exc}")
                    run_publish._progress(n, ctx)
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


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

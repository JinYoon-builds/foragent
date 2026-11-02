"""Publish a verified implementation: base refresh, re-verify, CI and merge."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent import run_merge_turn as merge_turn
    from foragent.policy import Decision
    from foragent.run_attempt import Implementation
    from foragent.run_base_move import BaseMove


def _publish(attempt: Implementation) -> str:
    run_publish.wait_for_overlap(attempt)
    return run_publish._publish_locked(attempt)


def _publish_locked(attempt: Implementation) -> str:
    # Leaving publication without merging always withdraws this run's ready entry (#712).
    # A merged run keeps its entry: the turn passes only once its run stops running.
    place = run_publish.merge_turn.seat(attempt.ctx, attempt.n, attempt.target_branch)
    outcome = error = None
    try:
        outcome = run_publish._publish_in_turn(attempt, place)
        return outcome
    except BaseException as exc:
        error = exc
        raise
    finally:
        if outcome != "merged":
            run_publish.merge_turn.withdraw(attempt.ctx, place, reason="publish-stopped",
                                            detail=run_publish.merge_turn.stopped_detail(outcome, error))


def _publish_in_turn(attempt: Implementation, place: merge_turn.Seat) -> str:
    from foragent.run_checks import _verify_implementation
    from foragent.run_conflicts import _block_rebuild, _merge_moved_base, _resolve_conflicts
    from foragent import harness_guard, review_pass_scope, run_group_check, run_group_ci, run_group_evidence, runner
    from foragent.run_review import _review_implementation
    from foragent.run_waits import activity

    # Only metadata is read live; instructions and checks always use the snapshot.
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    if attempt.review_hold and attempt.review_hold_reason == run_publish.Reason.TEST_HARNESS_CHANGED:
        return harness_guard.publish_hold(attempt)  # the hold wins over every publication check
    worktree, log = attempt.worktree, attempt.log
    issue = ctx.tracker.get_issue(n)
    title = f"{issue.title} (#{n})"
    run_publish.commit_exclusions(attempt, title)
    run_publish._progress(n, ctx)
    verified_head_sha = git.head_sha()
    review_pass_scope.bind(n, ctx, git)
    ci_cause = "initial"

    def finish_base() -> None:
        nonlocal verified_head_sha
        if runner._remaining(n, ctx) <= 0:
            raise run_publish.ExecutorStopped(run_publish._timeout(n, ctx))
        run_publish.commit_exclusions(attempt, title)
        verified_head_sha = git.head_sha()

    def keep_head(move: BaseMove) -> bool:
        """A result-neutral move keeps the verified head and its passed CI (#1688).

        Nothing is merged or pushed; only the accepted base moves. Later moves
        are captured from the same head, so their range includes this one.
        """
        nonlocal verified_base_sha
        if not run_publish.base_move_is_neutral(attempt, move):
            return False
        run_publish._append(log, f"{run_publish.REUSED}: {move.target_sha}")
        verified_base_sha = move.target_sha
        return True

    def refresh_base(move: BaseMove, conflicts: list[str] | None = None, *, cause: str | None = None,
                     neutral: bool | None = None) -> str:
        nonlocal ci_cause
        ci_cause = cause or run_group_check.refresh_cause(ctx, n)
        attempt.base_refresh = True  # This whole interval is one base-refresh record.
        try:
            with activity(n, ctx, "base-refresh", ci_cause):
                return refresh_moved_base(move, conflicts, neutral)
        finally:
            attempt.base_refresh = False

    def refresh_moved_base(move: BaseMove, conflicts: list[str] | None, neutral: bool | None) -> str:
        # Only the run's time budget (finish_base) bounds repeated base moves (#712).
        run_publish._phase(n, ctx, "checking")
        if neutral is None:
            neutral = conflicts is None and run_publish.base_move_is_neutral(attempt, move)
        run_publish._append(log, f"{run_publish.REUSED if neutral else run_publish.REVERIFY}: {move.target_sha}")
        # Exclude the merged base's paths from policy and patch replay. The
        # separate verified_base_sha advances only after verification/push.
        attempt.start_ref = move.target_sha
        if not neutral:
            # Protect what passes on the new base before merging it (#483).
            from foragent.run_implementation import follow_base
            follow_base(attempt)
        if conflicts is None:
            conflicts = _merge_moved_base(attempt, move.target_sha)
        changed = False
        if conflicts:
            _resolve_conflicts(attempt, conflicts)
        elif not neutral:
            merged_tree = git.staged_tree_sha()
            check_failure = _verify_implementation(attempt)
            if check_failure:
                raise run_publish.AttemptFailure(check_failure)
            # A check that edited tracked files left contents no review has seen.
            changed = git.staged_tree_sha() != merged_tree
        finish_base()
        if changed and ctx.config.reviewer != "none":
            run_publish._append(log, "Checks on the moved base changed the contents: reviewing them")
            _review_implementation(attempt)
            if attempt.failure and not attempt.review_hold:
                raise run_publish.AttemptFailure(attempt.failure)
        # A resolution rewrites code after the PASS, so it gets one review of that later change.
        # A hold returns through the publication path; other findings go back to the lane.
        if conflicts and ctx.config.reviewer != "none" and review_pass_scope.applies(n, ctx, git):
            _review_implementation(attempt)
            if attempt.failure and not attempt.review_hold:
                raise run_publish.AttemptFailure(attempt.failure)
        return move.target_sha

    target_sha = None
    if not attempt.review_hold:
        move = run_publish.fetch_move(attempt, verified_head_sha)
        # Publication can fetch a newer ref in another lane. Evidence belongs
        # to this captured base, including when it was already in the head.
        verified_base_sha = move.target_sha
        if not git.is_ancestor(move.target_sha, move.head_sha) and not keep_head(move):
            target_sha = refresh_base(move, neutral=False)
    if not attempt.review_hold and runner._remaining(n, ctx) <= 0:
        return run_publish._timeout(n, ctx)
    run_publish._check_publication(git, attempt.target_branch, None if attempt.review_hold else verified_base_sha)
    run_publish._push_branch(attempt)
    run_publish._progress(n, ctx)
    run_publish._append(log, f"Pushed verified HEAD: {verified_head_sha}")
    # The base this push's CI report measures; verified_base_sha is the base this
    # run accepts, which a proved neutral move advances without a push.
    measured_base_sha = None if attempt.review_hold else verified_base_sha
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

    def review_held() -> str:
        summary = (
            "Reviewer repeated a disputed finding after implementer rebuttals"
            if attempt.review_hold_reason == run_publish.Reason.REVIEW_DISPUTE
            else f"Reviewer still found defects after {attempt.review_failures} rounds"
        )
        details = f"{attempt.failure}\n\n{link}"
        run_publish.flag(ctx, n, attempt.review_hold_reason, summary, details)
        return "held"

    if attempt.review_hold:
        return review_held()

    def policy_hold(decision: Decision, batch_branch: str | None = attempt.batch_branch) -> str | None:
        if decision.kind == "hold":
            run_publish.flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if not run_publish._policy_allows(decision, attempt.target_branch, batch_branch):
            ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
            ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
            ctx.tracker.remove_labels(n, [run_publish.RUNNING])
            return "pr-open"

    if stopped := policy_hold(run_publish._merge_decision(
            n, ctx, git, ctx.tracker.pr_changed_files(pr), ctx.tracker.get_issue(n).labels)):
        return stopped

    def publish_refresh(target_sha: str) -> str | None:
        nonlocal pr_ready_at, ci_wait, verified_base_sha, measured_base_sha
        run_publish._check_publication(git, attempt.target_branch, target_sha)
        run_publish._push_branch(attempt)
        run_publish._progress(n, ctx)
        run_publish._append(log, f"Pushed verified HEAD: {verified_head_sha}")
        run_publish._update_open_pr(attempt, pr, title, pr_body())
        ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
        pr_ready_at = ctx.now().timestamp()
        ci_wait = run_publish._CIWait(pr_ready_at)
        verified_base_sha = measured_base_sha = target_sha
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return run_publish._hold_changed_head(n, ctx, link)
        if attempt.review_hold:
            return review_held()
        decision = run_publish._merge_decision(n, ctx, git, git.changed_paths(target_sha), ctx.tracker.get_issue(n).labels)
        return policy_hold(decision)

    def measured_later(sha: str) -> bool:
        """Main can move between our fetch and a push; CI then measures that later
        main. Accept it only as target history after the base we pushed against."""
        try:
            return (git.is_ancestor(measured_base_sha, sha)
                    and git.is_ancestor(sha, f"origin/{attempt.target_branch}"))
        except run_publish.GitError:
            return False

    def ci_failed() -> None:
        if attempt.rebuilt:
            logs = ctx.tracker.pr_failed_logs(pr, full_text_dir=run_publish.cuts.issue_dir(ctx.store.root, n))
            run_publish._hold_if_not_started(n, ctx, pr, logs)
            raise run_publish.ExecutorStopped(_block_rebuild(attempt, f"CI failed:\n{logs}"))
        run_publish._ci_failure(n, ctx, pr, verified_head_sha, attempt.target_branch)

    def reuse(target: str) -> bool:
        """Every base-move point asks this: does the joint pass verify this exact head on ``target``?"""
        nonlocal verified_base_sha, reused_base
        if run_group_check.group_limit(ctx.store, ctx.config)[0] <= 1:
            return False
        if (run_publish.enabled(ctx, attempt.target_branch)
                and run_group_ci.evidence(attempt, verified_head_sha, target) is None):
            return False
        if run_group_evidence.note(n, ctx, verified_head_sha, target, run_group_evidence.neutral_rule(attempt)):
            return False
        verified_base_sha = reused_base = target  # group-reuse: no refresh, recheck or push
        return True

    ci_wait = run_publish._CIWait(pr_ready_at)
    queued_status = None
    group_checked: set = set()
    reused_base = None
    # Base moves are bounded only by the run's time budget, which refresh_base checks.
    while True:
        run_publish._phase(n, ctx, "ci")
        with activity(n, ctx, "ci", ci_cause):
            stopped = run_publish._wait_for_ci(n, ctx, git, pr, verified_head_sha, pr_ready_at,
                                               ci_wait, ci_failed, queued_status,
                                               comparison_mode=run_publish.enabled(ctx, attempt.target_branch))
        if stopped:
            return stopped
        queued_status = None
        # Wait for the FIFO turn without re-verifying; CI is checked again on getting it (#712).
        waited, stopped = run_publish.merge_turn.await_turn(n, ctx, place, verified_head_sha, pr)
        if stopped:
            return stopped
        if waited:
            ci_wait = run_publish._CIWait(ctx.now().timestamp())
            continue

        # Refresh a base that already moved before taking the merge turn.
        # The final snapshot still detects changes during lock acquisition.
        ctx.git.fetch("origin", attempt.target_branch)
        target_sha = git.ref_sha(f"origin/{attempt.target_branch}")
        if target_sha != verified_base_sha and not reuse(target_sha):
            move = run_publish.capture_move(git, target_sha)
            if not keep_head(move):
                target_sha = refresh_base(move, neutral=False)
                if stopped := publish_refresh(target_sha):
                    return stopped
                continue
        if reused_base != verified_base_sha:  # Consecutive ready FIFO runs share one check, outside the lock.
            run_group_check.check(n, ctx, place, verified_head_sha, verified_base_sha, group_checked)

        comparison = None
        if run_publish.enabled(ctx, attempt.target_branch):
            comparison = run_group_ci.evidence(attempt, verified_head_sha, verified_base_sha)
            if comparison is None and reused_base == verified_base_sha:
                target_sha = refresh_base(run_publish.capture_move(git, verified_base_sha), cause="group-failed")
                reused_base = None
                if stopped := publish_refresh(target_sha):
                    return stopped
                continue
        if comparison is None and run_publish.enabled(ctx, attempt.target_branch):
            comparison = run_publish.require_comparison(attempt, pr, verified_head_sha, ci_wait.infrastructure,
                                                        base_sha=measured_base_sha, status=ci_wait.status,
                                                        deadline=ci_wait.deadline, later_base=measured_later)
            measured_base_sha = comparison.base_sha
            if verified_base_sha != measured_base_sha:
                comparison = comparison.follow(verified_base_sha)  # only keep_head moves it alone
            if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                return run_publish._hold_changed_head(n, ctx, link)

        run_publish.wait_for_overlap(attempt)
        failed_merge = None
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
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = run_publish.retitle_for_merge(
                    ctx, pr, title=title, current_title=attempt.pr_title,
                    base=attempt.target_branch, body=pr_body, note=lambda line: run_publish._append(log, line),
                )
                if not subject:
                    attempt.pr_title = title
                run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "merged")
                try:
                    ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha, **subject)
                except run_publish.TrackerError as exc:
                    run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "failed")
                    if exc.waits:
                        raise
                    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                        inputs.stop = lambda: run_publish._hold_changed_head(n, ctx, link)
                    else:
                        failed_merge = exc
                else:
                    run_group_evidence.record_merge(n, ctx, attempt.target_branch, inputs.base_sha, verified_head_sha)  # own merge provenance
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
                        if exc.waits:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [run_publish.READY, run_publish.RUNNING])
                    run_publish.board.sync_status(ctx, n, "Done")
                    if attempt.target_branch == attempt.batch_branch and attempt.parent is not None:
                        try:
                            run_publish._batch_pr(attempt.parent, ctx)
                        except (run_publish.GitError, run_publish.TrackerError) as exc:
                            if isinstance(exc, run_publish.TrackerError) and exc.waits:
                                raise
                            run_publish._append(log, f"Batch PR finalization deferred to tick: {exc}")
                    run_publish._progress(n, ctx)
                    return "merged"
        if failed_merge is not None:
            # Re-enter at the FIFO tail for a later turn instead of ending the run (#712).
            run_publish.merge_turn.rejoin_after_failed_merge(n, ctx, place, failed_merge, pr)
            queued_status = None
            continue
        if inputs.stop is not None:
            return inputs.stop()
        if stopped := policy_hold(inputs.decision, inputs.batch_branch):
            return stopped
        if not continue_merge or (move is None and not inputs.comparison_current):
            queued_status = inputs.status
            continue
        if move is not None:
            # A neutral move found at the lock gets another ordinary final check.
            if reuse(move.target_sha) or keep_head(move):
                continue
            # Pin the observed base; all merging/rechecks/pushes run unlocked.
            target_sha = refresh_base(move, neutral=False)
            if stopped := publish_refresh(target_sha):
                return stopped
            continue


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

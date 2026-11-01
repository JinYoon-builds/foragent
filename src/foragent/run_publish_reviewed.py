"""Publish an existing PR head after review-only verification."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent import run_merge_turn as merge_turn
    from foragent.git import GitRepo
    from foragent.policy import Decision
    from foragent.runner import RunContext
    from foragent.state import Approval


def _publish_reviewed_head(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, base_ref: str, base_sha: str,
    approval: Approval, approved: Callable[[], bool], batch_branch: str | None, parent: int | None,
    *, comparison_attempt=None, reverify: Callable[[str], str | None] | None = None,
) -> str:
    # The review-only merge takes the same FIFO turn as an implementation run (#712).
    place = run_publish.merge_turn.seat(ctx, n, pr.base)
    outcome = None
    try:
        outcome = run_publish._publish_reviewed_in_turn(
            n, ctx, git, pr, head, base_ref, base_sha, approval, approved, batch_branch, parent,
            place, comparison_attempt=comparison_attempt, reverify=reverify,
        )
        return outcome
    finally:
        if outcome != "merged":
            run_publish.merge_turn.withdraw(ctx, place)


def _publish_reviewed_in_turn(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, base_ref: str, base_sha: str,
    approval: Approval, approved: Callable[[], bool], batch_branch: str | None, parent: int | None,
    place: merge_turn.Seat, *, comparison_attempt=None, reverify: Callable[[str], str | None] | None = None,
) -> str:
    """Merge a reviewed head on its turn; ``reverify`` re-runs its checks after a base merge.

    A moved base is merged into the reviewed head, re-verified once per move and
    pushed (as the implementation path does after its review); without
    ``reverify`` or on a conflict it stops as base-changed.
    """
    from foragent import run_group_check, run_group_ci, run_group_evidence, runner

    worktree = git.root
    log = ctx.store.log_path(n)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    # A pass applies only to this head and these instructions. Neither the
    # reviewer nor an orchestrator verdict can waive the remaining checks.
    def unchanged() -> str | None:
        if not approved():
            return "hash-mismatch"
        if ctx.tracker.pr_head_sha(pr.number) != head:
            return run_publish._hold_changed_head(n, ctx, link)
        current = next((item for item in ctx.tracker.list_prs("open", pr.head)
                        if item.number == pr.number), None)
        # A moved target SHA is no stop here: the turn holder refreshes it (#712).
        if current is None or current.base != pr.base:
            return base_changed()
        if not run_publish._acceptance_matches(n, ctx, git, approval):
            return "hash-mismatch"
        return None

    def base_changed() -> str:
        run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
        return "base-changed"

    if stopped := unchanged():
        return stopped

    def refresh(target_sha: str) -> str | None:
        """Merge the moved base into the reviewed head, re-verify and push it."""
        nonlocal head, base_sha, started, ci_wait, queued_status
        if runner._remaining(n, ctx) <= 0:
            return run_publish._timeout(n, ctx)
        if reverify is None:
            return base_changed()
        run_publish._append(log, f"{run_publish.REVERIFY}: {target_sha}")
        try:
            git.merge_base(target_sha)
        except run_publish.GitError:
            if not git.conflict_paths():
                raise
            git.quit_merge()
            return base_changed()
        new_head = git.head_sha()
        if stopped := reverify(new_head):
            return stopped
        run_publish._check_publication(git, pr.base, target_sha)
        git.push_head(pr.head, lease=head)
        ctx.tracker.comment(pr.number, f"base merged before merge: {target_sha}")
        run_publish._append(log, f"Pushed verified HEAD: {new_head}")
        head, base_sha = new_head, target_sha
        started = ctx.now().timestamp()
        ci_wait, queued_status = run_publish._CIWait(started), None
        if ctx.tracker.pr_head_sha(pr.number) != head:
            return run_publish._hold_changed_head(n, ctx, link)
        return None

    def policy_hold(decision: Decision, current_batch: str | None = batch_branch) -> str | None:
        if decision.kind == "hold":
            run_publish.flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if not run_publish._policy_allows(decision, pr.base, current_batch):
            run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR is waiting for your review.", link)
            return "pr-open"
        return None

    if stopped := policy_hold(run_publish._merge_decision(
            n, ctx, git, sorted(set(git.changed_paths(base_sha)) | set(ctx.tracker.pr_changed_files(pr.number))),
            ctx.tracker.get_issue(n).labels)):
        return stopped
    started = ctx.now().timestamp()
    ci_wait = run_publish._CIWait(started)
    queued_status = None
    group_checked: set = set()
    reused_base = None
    # The reviewed head has no baseline of its own; run_base_move then judges locked
    # files against each commit's parent, as for an implementation without one.
    view = SimpleNamespace(n=n, ctx=ctx, git=git, approval=approval, baseline=None)

    def reuse(target: str) -> bool:
        """Every base-move point asks this: does the joint pass verify this exact head on ``target``?"""
        nonlocal base_sha, reused_base
        if ctx.config.merge_group_max <= 1:
            return False
        if comparison_attempt is not None and run_group_ci.evidence(view, head, target) is None:
            return False
        if run_group_evidence.note(n, ctx, head, target, run_group_evidence.neutral_rule(view)):
            return False
        base_sha = reused_base = target  # group-reuse: no merge, recheck or push
        return True

    while True:
        if stopped := run_publish._wait_for_ci(
            n, ctx, git, pr.number, head, started, ci_wait,
            lambda: run_publish._ci_failure(n, ctx, pr.number, head, pr.base), queued_status,
            comparison_mode=comparison_attempt is not None,
        ):
            return stopped
        waited, stopped = run_publish.merge_turn.await_turn(n, ctx, place, head)
        if stopped:
            return stopped
        if waited:
            # CI is checked again, with a fresh deadline, once the turn arrives.
            ci_wait, queued_status = run_publish._CIWait(ctx.now().timestamp()), None
            continue
        ctx.git.fetch("origin", pr.base)
        if (target_sha := ctx.git.ref_sha(base_ref)) != base_sha and not reuse(target_sha):
            if stopped := refresh(target_sha):
                return stopped
            continue
        if reused_base != base_sha:  # Consecutive ready FIFO runs share one check, outside the lock.
            run_group_check.check(n, ctx, place, head, base_sha, group_checked)
        comparison = None
        if comparison_attempt is not None:
            comparison = run_group_ci.evidence(view, head, base_sha)
            if comparison is None and reused_base == base_sha:
                reused_base = None
                if stopped := refresh(base_sha):
                    return stopped
                continue
        if comparison is None and comparison_attempt is not None:
            comparison = run_publish.require_comparison(comparison_attempt, pr.number, head, ci_wait.infrastructure,
                                                        base_sha=base_sha, status=ci_wait.status,
                                                        deadline=ci_wait.deadline)
            if stopped := unchanged():
                return stopped
        failed_merge = None
        with run_publish._merge_lock(n, ctx):
            inputs = run_publish._final_merge_inputs(
                n, ctx, git, pr.number, pr.head, pr.base, head, approval, started,
                approved_body_sha=approval.body_sha,
                comparison=comparison,
            )
            continue_merge = (inputs.stop is None and run_publish._policy_allows(inputs.decision, pr.base, inputs.batch_branch)
                              and inputs.status in {"pass", "none"} and inputs.comparison_current)
            # A base that moved during lock acquisition is refreshed unlocked, like the implementation path.
            moved = continue_merge and inputs.base_sha != base_sha
            if continue_merge and not moved:
                run_publish._check_publication(git, pr.base, inputs.base_sha)
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = run_publish.retitle_for_merge(
                    ctx, pr.number, title=title, current_title=pr.title, base=pr.base,
                    body=lambda: pr.body, note=lambda line: run_publish._append(log, line),
                )
                pr = run_publish.replace(pr, title=title)
                run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "merged")
                try:
                    ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head, **subject)
                except run_publish.TrackerError as exc:
                    run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "failed")
                    if exc.is_rate_limit:
                        raise
                    if ctx.tracker.pr_head_sha(pr.number) != head:
                        inputs.stop = lambda: run_publish._hold_changed_head(n, ctx, link)
                    else:
                        failed_merge = exc
                else:
                    run_group_evidence.record_merge(n, ctx, pr.base, inputs.base_sha, head)  # own merge provenance
                    ctx.store.record_merge(n, ctx.now())
                    if run_publish.review_mode_for(inputs.paths, ctx.config) == "fast":
                        runner.after_merge(ctx, pr.number, pr.base)
                    try:
                        if pr.base == batch_branch:
                            ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr.number})")
                        else:
                            ctx.tracker.close_issue(n)
                    except run_publish.TrackerError as exc:
                        if exc.is_rate_limit:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [run_publish.READY, run_publish.RUNNING, run_publish.BLOCKED])
                    if pr.base == batch_branch and parent is not None:
                        try:
                            run_publish._batch_pr(parent, ctx)
                        except (run_publish.GitError, run_publish.TrackerError) as exc:
                            if isinstance(exc, run_publish.TrackerError) and exc.is_rate_limit:
                                raise
                            run_publish._append(log, f"Batch PR finalization deferred to tick: {exc}")
                    return "merged"
        if failed_merge is not None:
            # Re-enter at the FIFO tail for a later turn instead of ending the run (#712).
            run_publish.merge_turn.rejoin_after_failed_merge(n, ctx, place, failed_merge)
            queued_status = None
            continue
        if inputs.stop is not None:
            return inputs.stop()
        if stopped := policy_hold(inputs.decision, inputs.batch_branch):
            return stopped
        if moved:
            if reuse(inputs.base_sha):
                continue
            if stopped := refresh(inputs.base_sha):
                return stopped
            continue
        queued_status = inputs.status


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

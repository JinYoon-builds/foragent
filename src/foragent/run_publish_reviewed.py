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


def push_refresh(ctx: RunContext, n: int, git: GitRepo, branch: str, lease: str, head: str) -> None:
    """The one leased refresh push; GitHub server errors wait with this head and lease."""
    from foragent import orch_github

    orch_github.wait_out_push(ctx, n, lambda: git.push_head(branch, lease=lease),
                              head=head, lease=lease, branch=branch, kind="refresh")


def _reviewed_base_changed(n: int, ctx: RunContext, pr) -> str:
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
    return "base-changed"


def _refresh_reviewed_head(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, target_sha: str,
    reverify: Callable[[str], str | None] | None,
    *, before_push: Callable[[str], None] | None = None,
    prepare: Callable[[], None] | None = None,
    before_base_changed: Callable[[], None] | None = None,
) -> tuple[str, str | None]:
    """Carry review through a clean base merge, verifying its exact head before pushing."""
    from foragent import runner

    def base_changed():
        if before_base_changed is not None:
            before_base_changed()
        return run_publish._reviewed_base_changed(n, ctx, pr)

    if runner._remaining(n, ctx) <= 0:
        return head, run_publish._timeout(n, ctx)
    if reverify is None:
        return head, base_changed()
    run_publish._phase(n, ctx, "checking")
    if git.head_sha() != head or ctx.tracker.pr_head_sha(pr.number) != head:
        link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
        return head, run_publish._hold_changed_head(n, ctx, link)
    log = ctx.store.log_path(n)
    run_publish._append(log, f"{run_publish.REVERIFY}: {target_sha}")
    try:
        git.merge_base(target_sha)
    except run_publish.GitError:
        if not git.conflict_paths():
            raise
        git.quit_merge()
        return head, base_changed()
    if prepare is not None:
        prepare()
    new_head = git.head_sha()
    if stopped := reverify(new_head):
        return head, stopped
    run_publish._check_publication(git, pr.base, target_sha)
    if before_push is not None:
        before_push(new_head)
    push_refresh(ctx, n, git, pr.head, head, new_head)
    ctx.tracker.comment(pr.number, f"base merged before merge: {target_sha}")
    run_publish._append(log, f"Pushed verified HEAD: {new_head}")
    if ctx.tracker.pr_head_sha(pr.number) != new_head:
        link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
        return new_head, run_publish._hold_changed_head(n, ctx, link)
    return new_head, None


def _publish_reviewed_head(
    n: int, ctx: RunContext, git: GitRepo, pr, head: str, base_ref: str, base_sha: str,
    approval: Approval, approved: Callable[[], bool], batch_branch: str | None, parent: int | None,
    *, comparison_attempt=None, reverify: Callable[[str], str | None] | None = None,
) -> str:
    # The review-only merge takes the same FIFO turn as an implementation run (#712).
    place = run_publish.merge_turn.seat(ctx, n, pr.base)
    outcome = error = None
    try:
        outcome = run_publish._publish_reviewed_in_turn(
            n, ctx, git, pr, head, base_ref, base_sha, approval, approved, batch_branch, parent,
            place, comparison_attempt=comparison_attempt, reverify=reverify,
        )
        return outcome
    except BaseException as exc:
        error = exc
        raise
    finally:
        if not getattr(ctx.config, "merge_owner", False) and outcome != "merged":
            run_publish.merge_turn.withdraw(ctx, place, reason="publish-stopped",
                                            detail=run_publish.merge_turn.stopped_detail(outcome, error))


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
    from foragent.run_waits import activity
    from foragent.ci_mode import OlderMeasuredBase

    log = ctx.store.log_path(n)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    # A pass applies only to this head and these instructions. Neither the
    # reviewer nor an orchestrator verdict can waive the remaining checks.
    def unchanged() -> str | None:
        if not approved():
            return "hash-mismatch"
        if ctx.tracker.pr_head_sha(pr.number) != head:
            return run_publish._hold_changed_head(n, ctx, link)
        current = (ctx.tracker.get_pr(pr.number) if getattr(ctx.config, "merge_owner", False) else
                   next((item for item in ctx.tracker.list_prs("open", pr.head) if item.number == pr.number), None))
        # A moved target SHA is no stop here: the turn holder refreshes it (#712).
        if (current is None or current.base != pr.base
                or getattr(ctx.config, "merge_owner", False) and (current.state != "open" or current.merged_at)):
            return run_publish._reviewed_base_changed(n, ctx, pr)
        if not run_publish._acceptance_matches(n, ctx, git, approval):
            return "hash-mismatch"
        return None

    if stopped := unchanged():
        return stopped

    ci_cause = "initial"

    def refresh(target_sha: str, cause: str | None = None) -> str | None:
        """Merge the moved base into the reviewed head, re-verify and push it."""
        nonlocal head, base_sha, started, ci_wait, queued_status, ci_cause
        ci_cause = cause or run_group_check.refresh_cause(ctx, n)
        with activity(n, ctx, "base-refresh", ci_cause):
            head, stopped = run_publish._refresh_reviewed_head(n, ctx, git, pr, head, target_sha, reverify)
        if stopped:
            return stopped
        base_sha = target_sha
        started = ctx.now().timestamp()
        ci_wait, queued_status = run_publish._CIWait(started), None
        return None

    def policy_hold(decision: Decision, current_batch: str | None = batch_branch) -> str | None:
        if getattr(ctx.config, "merge_owner", False) and ctx.merge_authorization is not None:
            return None  # This reviewed head was explicitly authorized by orch.merge.
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
    refreshed_pairs = set()

    def older_base(reported: str) -> bool:
        try:
            return ((reported, base_sha) not in refreshed_pairs
                    and git.is_ancestor(reported, base_sha)
                    and not git.is_ancestor(base_sha, head))
        except run_publish.GitError:
            return False  # Unproved ancestry keeps the existing CI wait.

    def refresh_older(reported: str) -> str | None:
        refreshed_pairs.add((reported, base_sha))
        run_publish._append(log, f"CI measured an older base: {reported} (expected {base_sha}); refreshing once")
        return refresh(base_sha)

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
        if run_group_check.group_limit(ctx.store, ctx.config)[0] <= 1:
            return False
        if comparison_attempt is not None and run_group_ci.evidence(view, head, target) is None:
            return False
        if run_group_evidence.note(n, ctx, head, target, run_group_evidence.neutral_rule(view)):
            return False
        base_sha = reused_base = target  # group-reuse: no merge, recheck or push
        return True

    while True:
        run_publish._phase(n, ctx, "ci")
        with activity(n, ctx, "ci", ci_cause):
            stopped = run_publish._wait_for_ci(
                n, ctx, git, pr.number, head, started, ci_wait,
                lambda: run_publish._ci_failure(n, ctx, pr.number, head, pr.base), queued_status,
                comparison_mode=comparison_attempt is not None,
            )
        if stopped:
            return stopped
        if getattr(ctx.config, "merge_owner", False):
            from foragent.merge_owner import enqueue

            comparison = None
            if comparison_attempt is not None:
                try:
                    comparison = run_publish.require_comparison(
                        comparison_attempt, pr.number, head, ci_wait.infrastructure,
                        base_sha=base_sha, status=ci_wait.status, deadline=ci_wait.deadline,
                        older_base=older_base,
                    )
                except OlderMeasuredBase as moved:
                    if stopped := refresh_older(moved.reported):
                        return stopped
                    continue
            else:
                from foragent import ci_local, ci_mode

                local = ci_mode.review_checks(n, ctx, git, pr.base)
                ci_local.fallback_comparison(local, base_sha)
            return enqueue(ctx, n, git, pr.number, pr.head, pr.base, head, base_sha,
                           approval, started, comparison)
        waited, stopped = run_publish.merge_turn.await_turn(n, ctx, place, head, pr.number)
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
                if stopped := refresh(base_sha, "group-failed"):
                    return stopped
                continue
        if comparison is None and comparison_attempt is not None:
            try:
                comparison = run_publish.require_comparison(comparison_attempt, pr.number, head, ci_wait.infrastructure,
                                                            base_sha=base_sha, status=ci_wait.status,
                                                            deadline=ci_wait.deadline, older_base=older_base)
            except OlderMeasuredBase as moved:
                if stopped := refresh_older(moved.reported):
                    return stopped
                continue
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
                from foragent.run_pr_body import merge_retitle_body
                subject = run_publish.retitle_for_merge(
                    ctx, pr.number, title=title, current_title=pr.title, base=pr.base,
                    body=lambda: merge_retitle_body(pr.head, ctx), note=lambda line: run_publish._append(log, line),
                )
                pr = run_publish.replace(pr, title=title)
                try:
                    with orch_stop.merging(ctx, n):  # A stop during the retitle merges nothing (#1491).
                        run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "merged")
                        ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head, **subject)
                except run_publish.TrackerError as exc:
                    run_publish.merge_turn.record_turn(ctx, place, inputs.base_sha, "failed")
                    if exc.waits:
                        raise
                    if ctx.tracker.pr_head_sha(pr.number) != head:
                        inputs.stop = lambda: run_publish._hold_changed_head(n, ctx, link)
                    else:
                        failed_merge = exc
                else:
                    run_group_evidence.record_merge(n, ctx, pr.base, inputs.base_sha, head)  # own merge provenance
                    ctx.store.record_merge(n, ctx.now())
                    size_signal.record(n, ctx, git, approval, inputs.base_sha, head, pr.number,
                                       lambda line: run_publish._append(log, line))
                    if run_publish.review_mode_for(inputs.paths, ctx.config) == "fast":
                        runner.after_merge(ctx, pr.number, pr.base)
                    try:
                        if pr.base == batch_branch:
                            ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr.number})")
                        else:
                            ctx.tracker.close_issue(n)
                    except run_publish.TrackerError as exc:
                        if exc.waits:
                            raise
                        if ctx.tracker.current_issue(n).state != "closed":
                            raise
                    ctx.tracker.remove_labels(n, [run_publish.READY, run_publish.RUNNING, run_publish.BLOCKED])
                    if pr.base == batch_branch and parent is not None:
                        try:
                            run_publish._batch_pr(parent, ctx)
                        except (run_publish.GitError, run_publish.TrackerError) as exc:
                            if isinstance(exc, run_publish.TrackerError) and exc.waits:
                                raise
                            run_publish._append(log, f"Batch PR finalization deferred to tick: {exc}")
                    return "merged"
        if failed_merge is not None:
            # Re-enter at the FIFO tail for a later turn instead of ending the run (#712).
            run_publish.merge_turn.rejoin_after_failed_merge(n, ctx, place, failed_merge, pr.number)
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
from foragent import orch_stop, run_publish, size_signal  # noqa: E402

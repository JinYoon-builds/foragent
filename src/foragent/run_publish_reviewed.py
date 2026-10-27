"""Publish an existing PR head after review-only verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent.git import GitRepo
    from foragent.policy import Decision
    from foragent.runner import RunContext
    from foragent.state import Approval


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
            return run_publish._hold_changed_head(n, ctx, link)
        current = next((item for item in ctx.tracker.list_prs("open", pr.head)
                        if item.number == pr.number), None)
        ctx.git.fetch("origin", pr.base)
        if current is None or current.base != pr.base or ctx.git.ref_sha(base_ref) != base_sha:
            run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
            return "base-changed"
        if not run_publish._acceptance_matches(n, ctx, git, approval):
            return "hash-mismatch"
        return None

    if stopped := unchanged():
        return stopped

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
    while True:
        if stopped := run_publish._wait_for_ci(
            n, ctx, git, pr.number, head, started, ci_wait,
            lambda: run_publish._ci_failure(n, ctx, pr.number, head, pr.base), queued_status,
            comparison_mode=comparison_attempt is not None,
        ):
            return stopped
        comparison = None
        if comparison_attempt is not None:
            comparison = run_publish.require_comparison(comparison_attempt, pr.number, head, ci_wait.infrastructure,
                                                        base_sha=base_sha, status=ci_wait.status,
                                                        deadline=ci_wait.deadline)
            if stopped := unchanged():
                return stopped
        with run_publish._merge_lock(n, ctx):
            inputs = run_publish._final_merge_inputs(
                n, ctx, git, pr.number, pr.head, pr.base, head, approval, started, base_sha=base_sha,
                approved_body_sha=approval.body_sha,
                comparison=comparison,
            )
            continue_merge = (inputs.stop is None and run_publish._policy_allows(inputs.decision, pr.base, inputs.batch_branch)
                              and inputs.status in {"pass", "none"} and inputs.comparison_current)
            if continue_merge:
                run_publish._check_publication(git, pr.base, inputs.base_sha)
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                subject = run_publish.retitle_for_merge(
                    ctx, pr.number, title=title, current_title=pr.title, base=pr.base,
                    body=lambda: pr.body, note=lambda line: run_publish._append(log, line),
                )
                pr = run_publish.replace(pr, title=title)
                try:
                    ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head, **subject)
                except run_publish.TrackerError as exc:
                    if exc.is_rate_limit:
                        raise
                    if ctx.tracker.pr_head_sha(pr.number) != head:
                        inputs.stop = lambda: run_publish._hold_changed_head(n, ctx, link)
                    else:
                        raise
                else:
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
        if inputs.stop is not None:
            return inputs.stop()
        if stopped := policy_hold(inputs.decision, inputs.batch_branch):
            return stopped
        queued_status = inputs.status


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

"""The final merge lock, its re-read inputs and the merge retitle."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from foragent.policy import Decision
from foragent.run_checks_approved import acceptance_mismatches
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent.ci_mode import ComparisonEvidence
    from foragent.run_group_ci import GroupComparisonEvidence
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.state import Approval


@contextmanager
def _merge_lock(n: int, ctx: RunContext):
    from foragent import capacity
    from foragent.run_waits import activity

    while True:
        # Budget waits happen unlocked. A contended merge lock releases this slot again.
        run_publish._phase(n, ctx, "merging")
        started = None
        with run_publish.ExitStack() as stack:
            def acquire():
                nonlocal started
                stack.enter_context(ctx.store.merge_lock())
                started = ctx.now()

            try:
                # Phase only if it waits: a free lock keeps the slot and adds no phase events.
                with activity(n, ctx, "merge-lock", "lock-contention"):
                    run_publish._credited_wait(n, ctx, acquire, "merge-lock-wait", eager=False, resume=False)
                if not capacity.phase_holds_slot(ctx.store.get_run(n).phase):
                    continue  # A new start took the freed slot: give the lock back.
                yield
                return
            finally:
                if started is not None:
                    ctx.store.record_merge_lock(n, (ctx.now() - started).total_seconds())


def _policy_allows(decision: Decision) -> bool:
    return decision.kind == "merge"


@dataclass
class _MergeInputs:
    # A stop is deferred so flagging/invalidation happens after releasing the lock.
    stop: Callable[[], str] | None = None
    base_sha: str = ""
    paths: list[str] = field(default_factory=list)
    decision: Decision = Decision("merge")
    status: str = "pending"
    comparison_current: bool = True


def _final_merge_inputs(
    n: int, ctx: RunContext, git: GitRepo, pr: int, branch: str, base: str,
    head: str, approval: Approval, ready_at: float, *, base_sha: str | None = None,
    approved_body_sha: str | None = None,
    comparison: ComparisonEvidence | GroupComparisonEvidence | None = None,
) -> _MergeInputs:
    """Read every mutable prerequisite after acquiring the final merge lock.

    Re-read approval/labels (and live body for review-only), PR state/branches, local and remote head,
    fetched target SHA, approved acceptance blobs on head and configured base,
    local and tracker paths, current policy config, comparison evidence,
    CI (including workflow registration grace), and the remaining run budget. Verification/review
    evidence stays pinned to the supplied head and approval snapshot.
    """
    link = f"https://github.com/{ctx.repo}/pull/{pr}"

    def invalid_approval() -> str:
        run_publish.invalidate_approval(ctx, n, "Approval invalidated: issue, approval or acceptance files changed. Re-approve to run it.")
        return "hash-mismatch"

    def changed_base() -> str:
        run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
        return "base-changed"

    status = run_publish._ci_status(ctx, pr, git.root, ready_at)
    issue = ctx.tracker.get_issue(n)
    saved = ctx.store.load_approval(n)
    if (run_publish.APPROVED not in issue.labels
            or (approved_body_sha is not None and run_publish.body_hash(issue.body) != approved_body_sha)
            or saved is None or saved.body_sha != approval.body_sha
            or saved.acceptance_blobs != approval.acceptance_blobs):
        return run_publish._MergeInputs(stop=invalid_approval)
    current = next((item for item in ctx.tracker.list_prs("open", branch) if item.number == pr), None)
    if (current is None or current.head != branch or current.base != base
            or base != ctx.config.base_branch):
        return run_publish._MergeInputs(stop=changed_base)
    ctx.git.fetch("origin", base)
    target_sha = git.ref_sha(f"origin/{base}")
    if base_sha is not None and target_sha != base_sha:
        return run_publish._MergeInputs(stop=changed_base)
    if any(acceptance_mismatches(git, approval, ref) for ref in (head, target_sha)):
        return run_publish._MergeInputs(stop=invalid_approval)
    paths = sorted(set(git.changed_paths(target_sha)) | set(ctx.tracker.pr_changed_files(pr)))
    decision = run_publish._merge_decision(n, ctx, git, paths, issue.labels)
    # Bind this status and all verification evidence to the exact head we merge.
    if git.head_sha() != head or ctx.tracker.pr_head_sha(pr) != head:
        return run_publish._MergeInputs(stop=lambda: run_publish._hold_changed_head(n, ctx, link))
    from foragent import runner
    if runner._remaining(n, ctx) <= 0:
        return run_publish._MergeInputs(stop=lambda: run_publish._timeout(n, ctx))
    comparison_current = True
    if run_publish.enabled(ctx):
        comparison_current = comparison is not None and comparison.current(n, ctx, git, pr, head, target_sha, status)
        if comparison_current and comparison.infrastructure:
            # The unlocked comparison already handled this infrastructure state.
            status = "pass"
    return run_publish._MergeInputs(base_sha=target_sha, paths=paths, decision=decision, status=status,
                                    comparison_current=comparison_current)


def retitle_for_merge(
    ctx: RunContext, pr: int, *, title: str, current_title: str | None, base: str,
    body: Callable[[], str | None], note: Callable[[str], None],
) -> dict[str, str]:
    """Give the PR its issue-derived title before merging; return extra merge_pr arguments.

    Branch commit messages (WIP checkpoints) never decide the title. The squash
    subject is the PR title, so it is passed only when retitling failed (other
    than a rate limit): the merge then still uses the issue-derived subject. A PR with
    no composition record changes its title alone, so the subject is right and no body is written.
    """
    from foragent.run_pr_body import publish_comments

    if title != current_title:
        try:
            rendered = body()
            if rendered is None:
                note(f"PR #{pr} has no composition record; merging with subject {title!r} and no body write.")
                ctx.tracker.retitle_pr(pr, title=title)
                return {}
            ctx.tracker.update_pr(pr, base=base, title=title, body=rendered)
            publish_comments(ctx, pr, rendered)
        except run_publish.TrackerError as exc:
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
    except run_publish.PublicationError as exc:
        raise run_publish.AttemptFailure(str(exc), target_branch) from exc


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

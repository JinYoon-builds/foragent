"""The final merge lock, its re-read inputs and the batch PR."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from foragent.policy import Decision
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent.ci_mode import ComparisonEvidence
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.state import Approval


@contextmanager
def _merge_lock(n: int, ctx: RunContext):
    # Keep tick informed during an unbounded blocking flock acquisition.
    started = None
    try:
        with run_publish.ExitStack() as stack:
            def acquire():
                nonlocal started
                stack.enter_context(ctx.store.merge_lock())
                started = ctx.now()

            run_publish._credited_wait(n, ctx, acquire)
            yield
    finally:
        if started is not None:
            ctx.store.record_merge_lock(n, (ctx.now() - started).total_seconds())


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
    if current is None or current.head != branch or current.base != base:
        return run_publish._MergeInputs(stop=changed_base)
    parent = ctx.tracker.parent_of(n)
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    if base not in {ctx.config.base_branch, batch_branch}:
        return run_publish._MergeInputs(stop=changed_base)
    ctx.git.fetch("origin", base)
    target_sha = git.ref_sha(f"origin/{base}")
    if base_sha is not None and target_sha != base_sha:
        return run_publish._MergeInputs(stop=changed_base)
    configured_base_sha = target_sha
    if base != ctx.config.base_branch:
        ctx.git.fetch("origin", ctx.config.base_branch)
        configured_base_sha = git.ref_sha(f"origin/{ctx.config.base_branch}")
    for ref in (head, configured_base_sha):
        blobs = git.blob_shas(ref, approval.acceptance_blobs)
        if any(blobs.get(path) != sha for path, sha in approval.acceptance_blobs.items()):
            return run_publish._MergeInputs(stop=invalid_approval)
    paths = sorted(set(git.changed_paths(target_sha)) | set(ctx.tracker.pr_changed_files(pr)))
    decision = run_publish.merge_decision(changed_paths=paths, issue_labels=issue.labels, config=ctx.config,
                                          risk_always_human=run_publish._risk_always_human(ctx))
    # Bind this status and all verification evidence to the exact head we merge.
    if git.head_sha() != head or ctx.tracker.pr_head_sha(pr) != head:
        return run_publish._MergeInputs(stop=lambda: run_publish._hold_changed_head(n, ctx, link))
    from foragent import runner
    if runner._remaining(n, ctx) <= 0:
        return run_publish._MergeInputs(stop=lambda: run_publish._timeout(n, ctx))
    comparison_current = True
    if run_publish.enabled(ctx, base):
        comparison_current = comparison is not None and comparison.current(n, ctx, git, pr, head, target_sha, status)
        if comparison_current and comparison.infrastructure:
            # The unlocked comparison already handled this infrastructure state.
            status = "pass"
    return run_publish._MergeInputs(base_sha=target_sha, paths=paths, decision=decision, status=status,
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
    with run_publish.tempfile.TemporaryDirectory(prefix=f"foragent-batch-{parent}-", dir=ctx.store.root) as directory:
        worktree = run_publish.Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            ctx.git.at(worktree).check_publication(base)
        finally:
            ctx.git.remove_worktree(worktree)
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    # No closing keyword: the parent's completion belongs to the gate's order
    # aggregation (board_parents), which comments first and keeps a parent with
    # a not-planned child open. A merge here must not close it behind that.
    lines = [run_publish.text("batch_pr_parent", ctx.config.language, parent=parent), "", "## Children", ""]
    for child in sorted(children, key=lambda child: child.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        lines.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    issue = ctx.tracker.get_issue(parent)
    title = f"Batch: {issue.title} (#{parent})"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        if issue.state == "open":  # A completed order (#411) stays in Done.
            run_publish.board.sync_status(ctx, parent, "PR open")
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    if issue.state == "open":
        run_publish.board.sync_status(ctx, parent, "PR open")
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

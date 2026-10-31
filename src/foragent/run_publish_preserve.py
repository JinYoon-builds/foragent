"""Save stopped work as a WIP commit before its worktree is cleaned up."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent.actions import FlagContext
    from foragent.git import GitRepo
    from foragent.state import Approval, Run


def _preserve_failed_implementation(
    n: int, ctx: FlagContext, git: GitRepo, reason: str, *, approval: Approval | None,
    approved_ref: str = "HEAD", title: str | None = None, pushed_head: str | None = None,
    tracker_available: bool = True, push_branch: Callable[[], None] | None = None,
    owned_deletions: bool = False,
) -> None:
    """Save stopped work before cleanup, including after the runner has died.

    Deletions outside the run's recorded files are left out (#572), unless no
    other process can be removing files meanwhile: the runner's own save after
    its executor ended (it passes `push_branch`), or `owned_deletions` (`fa orch
    stop`'s save of a killed tree under the tick lock).
    """
    with ctx.store.worktree_lock(n):
        # Recheck only after acquiring the lock: a competing cleanup may have
        # removed the worktree while we waited. A parent repository is no save.
        if not (git.root / ".git").is_file():
            return
        try:
            git.check_worktree()
        except run_publish.GitError:
            return
        run_publish._preserve_locked_implementation(
            n, ctx, git, reason, approval=approval, approved_ref=approved_ref, title=title,
            pushed_head=pushed_head, tracker_available=tracker_available, push_branch=push_branch,
            owned_deletions=owned_deletions or push_branch is not None,
        )


def _check_wip_deletions(ctx: FlagContext, git: GitRepo, run: Run | None) -> list[str]:
    """Leave unowned deletions out of the index without discarding other work."""
    deletions = git.staged_deletions()
    if not deletions:
        return []
    patterns = []
    if run is not None:
        patterns.extend(run.declared_files or [])
        patterns.extend([run_publish.escape(path) for path in run.effective_files or []]
                        if run.files_committed else run.effective_files or [])
        patterns.extend(run_publish.escape(path) for path in [*(run.changed_files or []), *run.late_files])
    base = (run.files_base_branch if run is not None else None) or ctx.config.base_branch
    committed = git.committed_paths(f"origin/{base}")
    artifacts = git.artifact_paths()
    refused = sorted(path for path in deletions
                     if path not in committed
                     and not any(path == root or path.startswith(root + "/") for root in artifacts)
                     and not run_publish.any_overlap(ctx, [run_publish.escape(path)], patterns))
    git.restore_staged_paths("HEAD", refused)
    return refused


def _preserve_locked_implementation(
    n: int, ctx: FlagContext, git: GitRepo, reason: str, *, approval: Approval | None,
    approved_ref: str, title: str | None, pushed_head: str | None,
    tracker_available: bool, push_branch: Callable[[], None] | None, owned_deletions: bool = False,
) -> None:
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
    refused = [] if owned_deletions else run_publish._check_wip_deletions(ctx, git, run)
    if git.has_staged_changes():
        git.commit(f"WIP: {title or 'Issue'} (#{n}) [{reason}]")
    if refused:
        detail = "WIP preservation refused: deletions outside allowed paths: " + ", ".join(map(repr, refused[:10]))
        run_publish._append(ctx.store.log_path(n), detail)
        run_publish.append_event(ctx.store, ctx.now(), n, "wip-preservation-refused", detail)
    if git.head_sha() == pushed_head or not tracker_available:
        return
    try:
        pr = ctx.tracker.find_pr(branch)
    except run_publish.TrackerError as exc:
        run_publish._append(ctx.store.log_path(n), f"WIP kept on local branch {branch}; open PR lookup failed: {exc}")
        return
    # An unverified WIP must not replace the head seen by an open PR's CI/review.
    if pr is None:
        if not run_publish.orch_scope.may_write(ctx, n):  # Another person's issue now: no push (#1004).
            run_publish._append(ctx.store.log_path(n), f"WIP kept on local branch {branch}; the issue's owner changed")
            return
        if push_branch is not None:
            push_branch()
        else:
            git.push(branch)


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

"""Target replay, conflict resolution and the single rebuild budget."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from foragent.actions import flag
from foragent.git import GitError
from foragent.i18n import text
from foragent.policy import review_mode_for
from foragent.reasons import Reason
from foragent.run_attempt import ConflictBudgetExhausted, ExecutorStopped, PatchReplayError
from foragent.run_checks import _acceptance_matches, _append
from foragent.run_lane_skills import _inject_lane_skills

if TYPE_CHECKING:
    from foragent.run_attempt import Implementation


def _note_moved(attempt: Implementation, moved: list[tuple[str, str]]) -> None:
    lines = [f"{old} -> {new}" for old, new in moved]
    lines = [line for line in lines if line not in attempt.moved_files]
    if lines:
        attempt.moved_files.extend(lines)
        _append(attempt.log, text("moved_files_log", attempt.ctx.config.language, files="\n".join(lines)))


def _merge_moved_base(attempt: Implementation, target_sha: str) -> list[str]:
    """Merge a pinned base; let the caller release its lock before resolving conflicts."""
    _inject_lane_skills(attempt.ctx, attempt.n, attempt.git, target_sha)
    moved = attempt.git.moved_by(target_sha)
    try:
        attempt.git.merge_base(target_sha)
    except GitError:
        _note_moved(attempt, moved)
        conflicts = attempt.git.conflict_paths()
        if not conflicts:
            raise
        return conflicts
    _note_moved(attempt, moved)
    return []


def _recreate_worktree(attempt: Implementation) -> None:
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree = attempt.worktree
    artifacts = {name: path.read_bytes() for name in ("TRY.md", "REBUTTAL.md")
                 if (path := worktree / ".foragent" / name).is_file()}
    attempt.start_ref = git.ref_sha(attempt.start_ref)
    ctx.git.remove_worktree(worktree)
    ctx.git.add_worktree(worktree, attempt.branch, attempt.start_ref)
    ctx.store.update_run(n, merge_conflicts=[])
    _inject_lane_skills(ctx, n, git, attempt.start_ref)
    for name, content in artifacts.items():
        path = worktree / ".foragent" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _prepare_target(attempt: Implementation) -> bool:
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    if not attempt.batch_branch:
        return True
    paths = git.changed_paths(attempt.start_ref)
    mode = review_mode_for(paths, ctx.config)
    destination = attempt.batch_branch if mode == "batch" else ctx.config.base_branch if mode == "fast" else attempt.target_branch
    if destination == attempt.target_branch:
        return True
    # Path overrides can select batch after implementation or generated
    # files, or return to fast after a retry removes those paths.
    if destination == attempt.batch_branch:
        ctx.git.ensure_batch_branch(attempt.batch_branch, attempt.base_ref)
    with tempfile.NamedTemporaryFile() as patch:
        git.write_staged_patch(attempt.start_ref, Path(patch.name))
        attempt.start_ref = git.ref_sha(f"origin/{destination}")
        attempt.target_branch = destination
        _recreate_worktree(attempt)
        if not _acceptance_matches(n, ctx, git, attempt.approval):
            return False
        try:
            git.apply_staged_patch(Path(patch.name))
        except GitError as exc:
            raise PatchReplayError(str(exc)) from exc
    return True


def _recover_replay(attempt: Implementation, exc: PatchReplayError) -> str:
    # A three-way failure can leave unmerged index entries. Start the
    # next implementation attempt from the destination's clean tree.
    _recreate_worktree(attempt)
    attempt.paths = []
    return f"Could not apply child changes to {attempt.target_branch}: {exc}"


def _rebuild(attempt: Implementation, exc: ConflictBudgetExhausted) -> str:
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    log = attempt.log
    attempt.rebuilt = True
    _append(log, f"Rebuilding issue branch once from latest {attempt.target_branch}.\n{exc}")
    ctx.git.fetch("origin", attempt.target_branch)
    attempt.start_ref = git.ref_sha(f"origin/{attempt.target_branch}")
    if not _acceptance_matches(n, ctx, ctx.git, attempt.approval, attempt.start_ref):
        raise ExecutorStopped("hash-mismatch")
    # Bind every rewritten push to the remote head observed before rebuilding.
    # An empty lease means the branch must still be absent.
    attempt.rebuild_lease = git.remote_branch_sha(attempt.branch) or ""
    _append(log, f"Rebuild target: {attempt.target_branch} ({attempt.start_ref})")
    _recreate_worktree(attempt)
    attempt.paths = []
    run = ctx.store.get_run(n)
    ctx.store.update_run(n, attempts=run.attempts + 1)
    return (
        f"The earlier attempt could not be merged onto the moved base ({attempt.target_branch}). "
        "The issue branch and worktree have been rebuilt from the latest target branch. "
        "Implement the original instructions again, preserving the features now on the base."
    )


def _block_rebuild(attempt: Implementation, details: str) -> str:
    _append(attempt.log, f"Rebuilt attempt failed:\n{details}")
    flag(attempt.ctx, attempt.n, Reason.RETRIES_EXCEEDED, "Rebuilt implementation failed; no attempts remain.", details)
    return "retries-exceeded"


def _resolve_conflicts(attempt: Implementation, conflicts: list[str]) -> None:
    from foragent.run_checks import _verify_implementation
    from foragent.run_implementation import _implement

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    log = attempt.log
    conflicts = sorted(set(conflicts) | set(ctx.store.get_run(n).merge_conflicts))
    ctx.store.update_run(n, merge_conflicts=conflicts)
    title = f"{attempt.issue.title} (#{n})"
    pending_merge = git.merge_in_progress()
    check_failure = f"Merge conflict with {attempt.target_branch}: {', '.join(conflicts)}"
    while True:
        if attempt.rebuilt:
            raise ExecutorStopped(_block_rebuild(attempt, check_failure))
        if attempt.conflict_attempts >= ctx.config.conflict_retries:
            raise ConflictBudgetExhausted(check_failure)
        attempt.conflict_attempts += 1
        _append(log, f"Conflict attempt {attempt.conflict_attempts}/{ctx.config.conflict_retries}:\n{check_failure}")
        _implement(attempt, check_failure, merge_conflicts=conflicts)
        # Retain the merge's paths even if the executor stages them.
        markers = git.conflict_marker_paths(conflicts)
        if markers:
            check_failure = f"Conflict markers remain in: {', '.join(markers)}"
            continue
        git.stage_changes()
        if pending_merge:
            git.continue_merge()
            pending_merge = False
        elif git.has_staged_changes():
            git.commit(title)
        check_failure = _verify_implementation(attempt)
        markers = git.conflict_marker_paths(conflicts)
        if markers:
            check_failure = f"Conflict markers remain in: {', '.join(markers)}"
        if not check_failure:
            ctx.store.update_run(n, merge_conflicts=[])
            return


def _resume_implementation(attempt: Implementation) -> bool:
    from foragent.run_publish import _merge_lock

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    conflicts = ctx.store.get_run(n).merge_conflicts
    # Own-path detection and replay follow the current target; publication
    # keeps the base associated with verification in its own SHA snapshot.
    attempt.start_ref = git.ref_sha(f"origin/{attempt.target_branch}")
    if conflicts or not git.is_ancestor(attempt.start_ref, git.head_sha()):
        # A saved implementation must see the same target as its acceptance
        # baseline before the worker starts. Keep its history and reuse the
        # publish path's conflict resolution and rebuild budget.
        with _merge_lock(n, ctx):
            if not git.is_ancestor(attempt.start_ref, git.head_sha()):
                moved = git.moved_by(attempt.start_ref)
                try:
                    # A saved partial resolution still needs its marker checks,
                    # even when Git can merge the target without index conflicts.
                    git.merge_base(attempt.start_ref, commit=not conflicts)
                    _note_moved(attempt, moved)
                except GitError:
                    _note_moved(attempt, moved)
                    unmerged = git.conflict_paths()
                    if not unmerged:
                        raise
                    conflicts = sorted(set(conflicts) | set(unmerged))
            if conflicts:
                try:
                    _resolve_conflicts(attempt, conflicts)
                except ConflictBudgetExhausted as exc:
                    attempt.failure = _rebuild(attempt, exc)
                else:
                    return True
    return False

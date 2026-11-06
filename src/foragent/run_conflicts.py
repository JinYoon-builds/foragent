"""Base-move conflict resolution and the single rebuild budget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent.actions import flag
from foragent.git import GitError
from foragent.i18n import text
from foragent.reasons import Reason
from foragent.run_attempt import ConflictBudgetExhausted, ExecutorStopped
from foragent.run_checks import _acceptance_matches, _append
from foragent.run_lane_skills import _inject_lane_skills
from foragent.run_overlap import conflict_resolution, record_redo

if TYPE_CHECKING:
    from foragent.run_attempt import Implementation


def _note_moved(attempt: Implementation, moved: list[tuple[str, str]]) -> None:
    """Queue moved or split files for the next prompt; saved in the run so restarts keep them."""
    if not moved:
        return  # nothing to tell; the run record is left untouched
    n, store = attempt.n, attempt.ctx.store
    pending = store.get_run(n).moved_files
    lines = [line for line in dict.fromkeys(f"{old} -> {new}" for old, new in moved) if line not in pending]
    if lines:
        store.update_run(n, moved_files=[*pending, *lines])
        _append(attempt.log, text("moved_files_log", attempt.ctx.config.language, files="\n".join(lines)))


def _merge_moved_base(attempt: Implementation, target_sha: str) -> list[str]:
    """Merge a pinned base; let the caller release its lock before resolving conflicts."""
    from foragent.ci_mode import commit_exclusions, enabled

    ci = enabled(attempt.ctx)
    _inject_lane_skills(attempt.ctx, attempt.n, attempt.git, target_sha)
    moved = attempt.git.moved_by(target_sha)
    try:
        attempt.git.merge_base(target_sha, commit=not ci)
    except GitError:
        _note_moved(attempt, moved)
        conflicts = attempt.git.conflict_paths()
        if not conflicts:
            raise
        return conflicts
    _note_moved(attempt, moved)
    if ci and attempt.git.merge_in_progress():
        commit_exclusions(attempt, f"{attempt.issue.title} (#{attempt.n})")
    return []


def _recreate_worktree(attempt: Implementation) -> None:
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree = attempt.worktree
    artifacts = {name: path.read_bytes() for name in ("PR.json", "TRY.md", "REBUTTAL.md", "SEARCHED.md")
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


def _own_paths(attempt: Implementation, target: str | None = None) -> list[str]:
    """The PR's own files: paths changed from the merge base to the branch head."""
    git = attempt.git
    if target is None:
        target = "MERGE_HEAD" if git.merge_in_progress() else attempt.start_ref
    try:
        base = git._call(["merge-base", target, "HEAD"]).strip()
        output = git._call(["diff", "--name-only", "--no-renames", "-z", base, "HEAD"])
    except GitError:
        return []
    return [path for path in output.split("\0") if path]


def _unresolved_conflicts(git, conflicts: list[str]) -> list[str]:
    """Conflict paths still unresolved: marker files, plus unmerged paths markers cannot show."""
    unmerged = set(git.conflict_paths()) & set(conflicts)
    unreadable = {name for name in unmerged
                  if (git.root / name).is_symlink() or not (git.root / name).is_file()}
    return sorted(set(git.conflict_marker_paths(conflicts)) | unreadable)


def _rebuild_reason(attempt: Implementation, exc: ConflictBudgetExhausted) -> str | None:
    """A fixed English reason when more than half of the PR's own files conflict, else None."""
    own = set(exc.own_paths)
    count = len(own & set(exc.conflicts))
    if not own or count * 2 <= len(own):
        return None
    return (f"{count}/{len(own)} of the PR's own files conflict with {attempt.target_branch} "
            f"after {attempt.ctx.config.conflict_retries} in-place resolution attempts")


def _record_rebuild(attempt: Implementation, reason: str) -> None:
    run = attempt.ctx.store.get_run(attempt.n)
    attempt.ctx.store.update_run(attempt.n, rebuild_count=run.rebuild_count + 1, rebuild_reason=reason)
    _append(attempt.log, f"Rebuild reason: {reason}")


def _stop_conflicts(attempt: Implementation, exc: ConflictBudgetExhausted) -> ExecutorStopped:
    """Keep the PR's work and hand the unresolved paths to the orchestrator."""
    from foragent.i18n import text

    language = attempt.ctx.config.language
    own = set(exc.own_paths)
    budget = attempt.ctx.config.conflict_retries
    if exc.conflicts:
        summary = text("conflict_stop", language, budget=budget,
                       count=len(own & set(exc.conflicts)), total=len(own))
        details = text("conflict_stop_files", language, paths=", ".join(exc.conflicts)) + f"\n\n{exc}"
    else:
        summary = text("conflict_stop_checks", language, budget=budget)
        details = str(exc)
    _append(attempt.log, f"{summary}\n{details}")
    flag(attempt.ctx, attempt.n, Reason.RETRIES_EXCEEDED, summary, details)
    return ExecutorStopped("retries-exceeded", details)


def _rebuild_after_conflicts(attempt: Implementation, exc: ConflictBudgetExhausted) -> str:
    """Rebuild from scratch only when more than half of the PR's own files conflict."""
    reason = _rebuild_reason(attempt, exc)
    if reason is None:
        raise _stop_conflicts(attempt, exc)
    _record_rebuild(attempt, reason)
    return _rebuild(attempt, exc)


def _rebuild(attempt: Implementation, exc: ConflictBudgetExhausted) -> str:
    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    log = attempt.log
    record_redo(attempt)
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


def _resolve_conflicts(
    attempt: Implementation, conflicts: list[str], *,
    own_paths: list[str] | None = None, failure: str | None = None,
) -> None:
    conflicts = sorted(set(conflicts) | set(attempt.ctx.store.get_run(attempt.n).merge_conflicts))
    with conflict_resolution(attempt, conflicts):
        _resolve_conflicts_inner(attempt, conflicts, own_paths=own_paths, failure=failure)


def _resolve_conflicts_inner(
    attempt: Implementation, conflicts: list[str], *,
    own_paths: list[str] | None = None, failure: str | None = None,
) -> None:
    from foragent.ci_mode import commit_exclusions, enabled
    from foragent.run_checks import _verify_implementation
    from foragent.run_implementation import _implement

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    log = attempt.log
    ctx.store.update_run(n, merge_conflicts=conflicts)
    title = f"{attempt.issue.title} (#{n})"
    pending_merge = git.merge_in_progress()
    own = _own_paths(attempt) if own_paths is None else own_paths
    check_failure = (f"{failure}\n" if failure else "") + (
        f"Merge conflict with {attempt.target_branch}: {', '.join(conflicts)}")
    while True:
        if attempt.rebuilt:
            raise ExecutorStopped(_block_rebuild(attempt, check_failure))
        if attempt.conflict_attempts >= ctx.config.conflict_retries:
            # Judge the rebuild on what is still unresolved now, not on the initial conflict list.
            raise ConflictBudgetExhausted(check_failure, _unresolved_conflicts(git, conflicts), own)
        attempt.conflict_attempts += 1
        _append(log, f"Conflict attempt {attempt.conflict_attempts}/{ctx.config.conflict_retries}:\n{check_failure}")
        _implement(attempt, check_failure, merge_conflicts=conflicts)
        # Retain the merge's paths even if the executor stages them.
        markers = git.conflict_marker_paths(conflicts)
        if markers:
            check_failure = f"Conflict markers remain in: {', '.join(markers)}"
            continue
        git.stage_changes()
        if enabled(ctx):
            commit_exclusions(attempt, title)
            pending_merge = False
        elif pending_merge:
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
    from foragent.ci_mode import commit_exclusions, enabled

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    conflicts = ctx.store.get_run(n).merge_conflicts
    # Own-path detection follows the configured base; publication
    # keeps the base associated with verification in its own SHA snapshot.
    attempt.start_ref = git.ref_sha(f"origin/{attempt.target_branch}")
    if conflicts or not git.is_ancestor(attempt.start_ref, git.head_sha()):
        # A saved implementation must see the same target as its acceptance
        # baseline before the worker starts. Keep its history and reuse the
        # publish path's conflict resolution and rebuild budget.
        # Preparation stays unlocked; publication rechecks the verified base
        # and all merge inputs under its final short merge lock.
        if not git.is_ancestor(attempt.start_ref, git.head_sha()):
            moved = git.moved_by(attempt.start_ref)
            try:
                # A saved partial resolution still needs its marker checks,
                # even when Git can merge the target without index conflicts.
                ci = enabled(ctx)
                git.merge_base(attempt.start_ref, commit=not conflicts and not ci)
                if ci and not conflicts and git.merge_in_progress():
                    commit_exclusions(attempt, f"{attempt.issue.title} (#{n})")
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
                attempt.failure = _rebuild_after_conflicts(attempt, exc)
            else:
                return True
    return False

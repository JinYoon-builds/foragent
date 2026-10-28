"""The lane dream's branch and PR (#189): read the open PR's memory, never lose it, publish once."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foragent.orch_dream import last_published

if TYPE_CHECKING:
    from foragent.runner import RunContext

GIT_IDENTITY = ["-c", "user.name=foragent", "-c", "user.email=foragent@localhost"]


def _dream_worktree(ctx: RunContext, lane: str, worktree: Path, directory: str):
    """Start from the lane's dream branch when the clone has one, with the base merged in.

    The branch's memory commits always stay. On a conflict it is replaced from the base only
    when its memory is already in the base's history (a squash-merged PR, then a base edit);
    otherwise the dream ends as error and the branch and its PR stay untouched. Local refs only.
    """
    branch, base = f"foragent/dream-{lane}", f"origin/{ctx.config.base_branch}"
    remote = f"refs/remotes/origin/{branch}"
    try:
        pushed = ctx.git._call(["rev-parse", "--verify", "--quiet", remote]).strip() or None
    except Exception:
        pushed = None
    ctx.git.add_worktree(worktree, branch, remote if pushed else base)
    git = ctx.git.at(worktree)
    if pushed:
        try:
            git._call([*GIT_IDENTITY, "merge", "--no-edit", base])
        except Exception:
            git._call(["merge", "--abort"])
            if not _memory_in_base(git, remote, base, directory):
                raise RuntimeError(f"{branch} holds unmerged memory that conflicts with {base}; "
                                   "its PR is left untouched until the conflict is resolved")
            ctx.git.remove_worktree(worktree)
            ctx.git.add_worktree(worktree, branch, base)
            return ctx.git.at(worktree), pushed, True
    return git, pushed, False


def _memory_in_base(git, branch: str, base: str, directory: str, depth: int = 500) -> bool:
    """Whether the base took the branch's memory directory, as a tree, after the branch forked.

    Only base commits after the merge base count: a tree the base had before the fork (an
    open PR reverting a base edit, A->B on the base and B->A on the branch) is an unmerged edit.
    """
    try:
        tree = git._call(["rev-parse", f"{branch}:{directory}"]).strip()
    except Exception:
        return True  # The branch holds no memory directory, so nothing of it can be lost.
    fork = git._call(["merge-base", branch, base]).strip()
    for commit in git._call(["log", f"--max-count={depth}", "--format=%H", f"{fork}..{base}", "--",
                             directory]).split():
        try:
            if git._call(["rev-parse", f"{commit}:{directory}"]).strip() == tree:
                return True
        except Exception:
            continue
    return False


def _unpublished(store, git, lane: str, pushed: str | None, base: str, directory: str) -> bool:
    """Whether the branch holds pushed memory no completed dream published as a PR.

    A push whose PR creation failed leaves a head unlike the last ``published`` one; a
    merged PR's branch has no memory difference from the base. Local facts only.
    """
    if pushed is None or pushed == last_published(store, lane):
        return False
    return bool(git._call(["diff", "--name-only", base, "HEAD", "--", directory]).strip())


def _publish(ctx: RunContext, git, lane: str, memory: str, count: int, *, replace: bool = False,
             start: str | None = None) -> int:
    """Push the committed memory edit and open or update the lane's dream PR.

    An open dream PR keeps its earlier, unmerged edits: this dream's commit is merged
    onto that branch and pushed with a lease on the head it merged, never forced over it.
    A stale branch (``replace``: it conflicted with the base and its memory is already in the
    base's history) is replaced by this dream, with a lease on the head it started from.
    """
    branch, base = f"foragent/dream-{lane}", ctx.config.base_branch
    title = f"Lane {lane} memory: {ctx.now().astimezone().date().isoformat()}"
    pr = ctx.tracker.find_pr(branch)  # The one GitHub lookup, made only when memory changed.
    if pr is None:
        # The cached origin ref may be stale (the branch was deleted after its PR merged), so the
        # remote head is read once: absent -> create it; the head this dream started from -> lease
        # on it; any other head -> leave it, since this dream has not read its memory.
        listed = git._call(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).split()
        remote = listed[0] if listed else None
        if remote is not None and remote != start:
            raise RuntimeError(f"{branch} moved to {remote} while this dream started from {start}; not overwritten")
        git.push(branch, lease=remote or "")
    else:
        git.fetch("origin", branch)
        head = git._call(["rev-parse", f"refs/remotes/origin/{branch}"]).strip()
        if not replace:
            try:
                git._call([*GIT_IDENTITY, "merge", "--no-edit", "-m", title, f"refs/remotes/origin/{branch}"])
            except Exception:
                git._call(["merge", "--abort"])
                raise
        # A replaced branch is leased on the head the dream saw when it started, never on a head
        # fetched later, whose newer memory this dream has not read.
        git.push(branch, lease=(start or "") if replace else head)
    body = (f"The {lane} lane's dream tidied `{memory}` ({count} lines added or removed).\n\n"
            "Only the lane's memory directory changes. The orchestrator merges it when CI passes "
            "(skills/fa-orchestrator/references/dreams.md).")
    if pr is None:
        return ctx.tracker.create_pr(head=branch, base=base, title=title, body=body)
    ctx.tracker.update_pr(pr, base=base, title=title, body=body)
    return pr

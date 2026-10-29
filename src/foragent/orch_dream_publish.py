"""The lane dream's branch and PR (#189): every dream starts from the base; an open PR is never touched.

A lane whose dream PR is open does not dream (the tick checks it), so no dream ever merges into,
resets or replaces an open dream PR's branch. The worker's one pre-push lookup is the last guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.runner import RunContext


def _dream_worktree(ctx: RunContext, lane: str, worktree: Path):
    """The dream's worktree on foragent/dream-<lane>, always started from the base."""
    ctx.git.add_worktree(worktree, f"foragent/dream-{lane}", f"origin/{ctx.config.base_branch}")
    return ctx.git.at(worktree)


def _publish(ctx: RunContext, git, lane: str, memory: str, count: int) -> int:
    """Force-push the dream branch and open its PR; an open dream PR ends the dream instead.

    The push is leased on the remote head this worker read just before it, so a branch that
    changed meanwhile is never overwritten. A closed PR's branch holds nothing open.
    """
    branch, base = f"foragent/dream-{lane}", ctx.config.base_branch
    if (pr := ctx.tracker.find_pr(branch)) is not None:  # The one GitHub lookup, made only when memory changed.
        raise RuntimeError(f"dream PR #{pr} for lane {lane} is open; nothing was pushed")
    listed = git._call(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).split()
    git.push(branch, lease=listed[0] if listed else "")
    title = f"Lane {lane} memory: {ctx.now().astimezone().date().isoformat()}"
    body = (f"The {lane} lane's dream tidied `{memory}` ({count} lines added or removed).\n\n"
            "Only the lane's memory directory changes. The orchestrator merges it when CI passes "
            "(skills/fa-orchestrator/references/dreams.md).")
    return ctx.tracker.create_pr(head=branch, base=base, title=title, body=body)

"""The lane dream's branch and PR (#189): every dream starts from the base; an open PR is never touched.

A lane whose dream PR is open does not dream (the tick checks it), so no dream ever merges into,
resets or replaces an open dream PR's branch. The worker's one pre-push lookup is the last guard.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
from typing import TYPE_CHECKING

from foragent.lane_memory import CURATED, PENDING

if TYPE_CHECKING:
    from foragent.runner import RunContext


def _dream_worktree(ctx: RunContext, lane: str, worktree: Path):
    """The dream's worktree on foragent/dream-<lane>, always started from the base."""
    ctx.git.add_worktree(worktree, f"foragent/dream-{lane}", f"origin/{ctx.config.base_branch}")
    return ctx.git.at(worktree)


def check_base(git, base: str, memory: str, captured: dict[str, bytes]) -> None:
    """Raise unless the base still holds the memory files the dream read (D95).

    The dream folded a captured prefix of MEMORY.new.md into MEMORY.md. If the base changed
    either file since, the dream's diff could drop a late lesson or a hand edit; nothing is
    pushed, the base keeps every input, and the lane dreams again from the new base.
    """
    git.fetch("origin", base)
    folder = Path(memory).parent
    for name in (CURATED, PENDING):
        sha = git.blob_sha(f"origin/{base}", (folder / name).as_posix())
        current = git.blob_bytes(sha.encode()) if sha is not None else b""
        if current != captured.get(name, b""):
            raise RuntimeError(f"{folder / name} changed on {base} during the dream; nothing was pushed, "
                               "the lane dreams again from the new base")


def fingerprints(files: dict[str, bytes]) -> dict[str, str | None]:
    """Content digests of the two memory files; None for an absent file."""
    return {name: hashlib.sha256(files[name]).hexdigest() if name in files else None for name in (CURATED, PENDING)}


def memory_moved(git, base: str, head: str, folder: str, inputs: dict, outputs: dict) -> list[str]:
    """The memory files that no longer match what the dream read (on the base) or wrote (on its PR).

    ``inputs`` and ``outputs`` are the digests recorded when the dream published; they never move
    with Git history. A dream removes the pending lines it folded: merging it over a base that
    appended to MEMORY.new.md, or a PR rebased over such a base (union merge), would bring the
    folded lines back, and a changed MEMORY.md could lose a hand edit. Such a PR is closed and
    the lane dreams again.
    """
    git.fetch("origin", base)
    git.fetch("origin", head)
    moved = []
    for ref, expected, where in ((f"origin/{base}", inputs, base), (f"origin/{head}", outputs, head)):
        for name in (CURATED, PENDING):
            sha = git.blob_sha(ref, f"{folder}/{name}")
            found = hashlib.sha256(git.blob_bytes(sha.encode())).hexdigest() if sha is not None else None
            if name not in expected or found != expected[name]:
                moved.append(f"{folder}/{name} on {where}")
    return moved


def _publish(ctx: RunContext, git, lane: str, memory: str, count: int,
             captured: dict[str, bytes] | None = None) -> int:
    """Force-push the dream branch and open its PR; an open dream PR ends the dream instead.

    The push is leased on the remote head this worker read just before it, so a branch that
    changed meanwhile is never overwritten. A closed PR's branch holds nothing open.
    """
    branch, base = f"foragent/dream-{lane}", ctx.config.base_branch
    if captured is not None:
        check_base(git, base, memory, captured)
    if (pr := ctx.tracker.find_pr(branch)) is not None:  # The one GitHub lookup, made only when memory changed.
        raise RuntimeError(f"dream PR #{pr} for lane {lane} is open; nothing was pushed")
    listed = git._call(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).split()
    git.push(branch, lease=listed[0] if listed else "")
    title = f"Lane {lane} memory: {ctx.now().astimezone().date().isoformat()}"
    body = (f"The {lane} lane's dream tidied `{memory}` ({count} lines added or removed) and removed the "
            f"lessons it folded from `{Path(memory).with_name(PENDING)}`.\n\n"
            "Only the lane's memory directory changes. The orchestrator merges it when CI passes "
            "(skills/fa-orchestrator/references/dreams.md).")
    return ctx.tracker.create_pr(head=branch, base=base, title=title, body=body)

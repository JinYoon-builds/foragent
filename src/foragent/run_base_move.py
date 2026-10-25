"""Classify a moved base before merging it into a verified branch (#376).

A move is result-neutral only when every new base commit touches documentation
under docs/ or locked acceptance files that this run's acceptance baseline
excluded because another approved issue owns them and has still not merged. Any other path,
or a path this branch changed itself, re-verifies as before.

The decision reads only the move it will merge: the fixed commit range
``head_sha..target_sha`` captured once by the caller, the exact NUL-delimited
paths of every commit in that range, and the exclusion set the acceptance
baseline actually used. The caller then merges that same ``target_sha``.
Approvals and merge history may only narrow the neutral set further.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from foragent.run_baseline import unmerged_acceptance_owners

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.run_attempt import Implementation

REUSED = "base moved: only result-neutral paths; verification reused"
REVERIFY = "base moved: result-affecting paths; re-verifying"
NEUTRAL_DIRECTORY = "docs/"


@dataclass(frozen=True)
class BaseMove:
    """One observed base move, fixed to commit SHAs before it is judged or merged."""

    head_sha: str
    target_sha: str


def capture_move(git: GitRepo, target_sha: str) -> BaseMove:
    """Pin both ends of the range once; refs can move again before the merge."""
    return BaseMove(git.ref_sha("HEAD"), git.ref_sha(target_sha))


def fetch_move(attempt: Implementation, verified_head: str) -> BaseMove:
    """Keep the fetched base even when it is already in the verified head."""
    attempt.ctx.git.fetch("origin", attempt.target_branch)
    target = attempt.git.ref_sha(f"origin/{attempt.target_branch}")
    return BaseMove(verified_head, target)


def _nul_paths(output: str) -> list[str]:
    # Paths are taken verbatim between NUL bytes: a filename may start or end
    # with whitespace or newlines, so no character other than NUL is stripped.
    return [path for path in output.split("\0") if path != ""]


def moved_paths(git: GitRepo, move: BaseMove) -> set[str]:
    """Paths touched by each commit in the fixed range, not only by the net diff."""
    commits = git._call(["rev-list", f"{move.head_sha}..{move.target_sha}"]).split()
    paths: set[str] = set()
    for commit in commits:
        # -m compares merge commits with every parent; --root covers parentless
        # commits; --no-renames keeps both sides of a rename.
        paths.update(_nul_paths(git._call([
            "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--name-only", "--no-renames", "-z",
            commit,
        ])))
    return paths


def base_move_is_neutral(attempt: Implementation, move: BaseMove) -> bool:
    # Only files the verified baseline really skipped can be neutral; an issue
    # approved later does not remove its test from what was already verified.
    skipped = set(attempt.baseline.excluded) if attempt.baseline else set()
    # This issue's own locked files are verified by Done when.
    skipped -= set(attempt.approval.acceptance_blobs)
    owners = unmerged_acceptance_owners(attempt.ctx)
    pending_locked = {name for name in skipped if name in owners and attempt.n not in owners[name]}
    own = set(attempt.git.changed_paths(move.target_sha))
    return all(
        path not in own and (path.startswith(NEUTRAL_DIRECTORY) or path in pending_locked)
        for path in moved_paths(attempt.git, move)
    )

"""Classify a moved base before merging it into a verified branch (#376).

A move is result-neutral only when every new base commit touches documentation
under docs/ or locked acceptance files that this run's acceptance baseline
excluded because another approved issue owns them and has still not merged. Any other path,
or a path this branch changed itself, re-verifies as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent.run_baseline import unmerged_acceptance_owners

if TYPE_CHECKING:
    from foragent.run_attempt import Implementation

REUSED = "base moved: only result-neutral paths; verification reused"
REVERIFY = "base moved: result-affecting paths; re-verifying"
NEUTRAL_DIRECTORY = "docs/"


def moved_paths(attempt: Implementation, target_sha: str) -> set[str]:
    """Paths touched by each new base commit, not only by the net diff."""
    git = attempt.git
    # -m lists merge commits against every parent; --no-renames keeps both names.
    output = git._call([
        "log", "--format=", "--name-only", "--no-renames", "-m", "-z",
        f"{git.head_sha()}..{target_sha}",
    ])
    return {path.strip("\n") for path in output.split("\0") if path.strip("\n")}


def base_move_is_neutral(attempt: Implementation, target_sha: str) -> bool:
    # Only files the verified baseline really skipped can be neutral; an issue
    # approved later does not remove its test from what was already verified.
    skipped = set(attempt.baseline.excluded) if attempt.baseline else set()
    owners = unmerged_acceptance_owners(attempt.ctx)
    pending_locked = {name for name, numbers in owners.items() if name in skipped and attempt.n not in numbers}
    own = set(attempt.git.changed_paths(target_sha))
    return all(
        path not in own and (path.startswith(NEUTRAL_DIRECTORY) or path in pending_locked)
        for path in moved_paths(attempt, target_sha)
    )

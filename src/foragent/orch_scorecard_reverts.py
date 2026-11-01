"""Revert facts for `fa orch scorecard`: canonical revert commits on the default branch.

The fact is what `git revert` wrote, wherever it was made: a subject ``Revert "..."`` or
``Reapply "..."`` with a body line ``This reverts commit <sha>``. Each read asks local Git again,
so nothing is stored and nothing is fetched. ``None`` means the history could not be read, which
never proves that there were no reverts.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path

from foragent.git import GitError, GitRepo

SUBJECTS = ('Revert "', 'Reapply "')
BODY_LINE = "^This reverts commit [0-9a-f]{7,40}([^0-9a-zA-Z_]|$)"


def _commit_time(record: str) -> datetime | None:
    """The committer time of a ``<unix time>\\n<message>`` record, or None for another subject."""
    stamp, _, message = record.partition("\n")
    if not message.startswith(SUBJECTS):  # A squash can quote a revert's body line.
        return None
    return datetime.fromtimestamp(int(stamp), UTC)


def _in_repository(root: Path) -> bool:
    """Whether Git has anything to read here; without it the read starts no process at all."""
    if "GIT_DIR" in os.environ:  # Git reads that directory and ignores the path.
        return True
    start = Path(root).absolute()
    return any((folder / ".git").exists() for folder in (start, *start.parents))


def times(root: Path, base_branch: str) -> list[datetime] | None:
    """Committer times of every canonical revert reachable from ``origin/<base_branch>``."""
    if not _in_repository(root):
        return None
    git = GitRepo(root)
    try:
        # A shallow history is incomplete, so finding nothing in it proves nothing.
        if git._call(["rev-parse", "--is-shallow-repository"]).strip() != "false":
            return None
        # No --since or --first-parent: committer times can interleave, and a revert merged
        # from a side branch still reached the default branch. Python buckets the times.
        output = git._call(["log", "--no-show-signature", "-z", "--format=%ct%n%B",
                            "--extended-regexp", f"--grep={BODY_LINE}",
                            f"refs/remotes/origin/{base_branch}", "--"])
        found = [_commit_time(record) for record in output.split("\0") if record.strip()]
    except (GitError, ValueError, OverflowError, OSError):
        return None
    return [moment for moment in found if moment is not None]

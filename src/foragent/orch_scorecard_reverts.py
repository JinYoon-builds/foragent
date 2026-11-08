"""Revert facts for `fa orch scorecard`: canonical revert commits on the default branch.

The fact is what `git revert` wrote, wherever it was made: a subject ``Revert "..."`` or
``Reapply "..."`` with a body line ``This reverts commit <sha>``. Each read asks local Git again,
so nothing is stored and nothing is fetched. ``None`` means the history could not be read, which
never proves that there were no reverts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess
import threading
from typing import Callable

from foragent.git import GitError, GitRepo, in_repository

TIMEOUT_SECONDS: float | None = 120  # None: no deadline (the test world's setting).
SUBJECTS = ('Revert "', 'Reapply "')
BODY_LINE = "^This reverts commit [0-9a-f]{7,40}([^0-9a-zA-Z_]|$)"


def _commit_time(record: str) -> datetime | None:
    """The committer time of a ``<unix time>\\n<message>`` record, or None for another subject."""
    stamp, _, message = record.partition("\n")
    if not message.startswith(SUBJECTS):  # A squash can quote a revert's body line.
        return None
    return datetime.fromtimestamp(int(stamp), UTC)


def _runner(root: Path) -> Callable[[list[str]], str]:
    """Git with a deadline and no polling: ``subprocess.run(timeout=...)`` waits in a sleep loop."""
    def run(args: list[str]) -> str:
        command = ["git", *args]
        with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors="replace") as process:
            deadline = None if TIMEOUT_SECONDS is None else threading.Timer(TIMEOUT_SECONDS, process.kill)
            if deadline is not None:
                deadline.daemon = True
                deadline.start()
            try:
                output, errors = process.communicate()
            finally:
                if deadline is not None:
                    deadline.cancel()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output, errors)
        return output
    return run


def times(root: Path, base_branch: str) -> list[datetime] | None:
    """Committer times of every canonical revert reachable from ``origin/<base_branch>``."""
    if not in_repository(root):
        return None
    git = GitRepo(root, _runner(root))
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

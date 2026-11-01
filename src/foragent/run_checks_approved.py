"""What a person approved, checked the same way by the ordinary and person-approved merge paths.

Each caller keeps its own consequence: the ordinary path invalidates the approval or spends
its retry budget, the person-approved path only refuses the merge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import cuts
from foragent.git import ABSENT_BLOB
from foragent.issue import parse_body

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.run_checks import CheckResult
    from foragent.runner import RunContext
    from foragent.state import Approval

DONE_WHEN_SECONDS = 900


def acceptance_mismatches(git: GitRepo, approval: Approval, ref: str, *, absent_ok: bool = True) -> list[str]:
    """Locked paths whose bytes at ``ref`` differ from the approved ones.

    A snapshot records a path absent at approval as ``ABSENT_BLOB``, so that marker equals itself
    when the path is still absent. ``absent_ok=False`` refuses such a path as missing.
    """
    blobs = git.blob_shas(ref, approval.acceptance_blobs)
    return [path for path, sha in approval.acceptance_blobs.items()
            if blobs.get(path) != sha or (not absent_ok and sha == ABSENT_BLOB)]


def run_done_when(
    n: int, ctx: RunContext, command: str, worktree: Path, log: Path, timeout_s: float,
    *, slot: AbstractContextManager, progress: Callable[[], None] | None = None,
) -> CheckResult:
    """Run one approved Done when command; ``slot`` covers only the command, not its queue."""
    from foragent import runner

    return runner._check(command, worktree, log, timeout_s, progress=progress, slot=slot,
                         full_text_dir=cuts.issue_dir(ctx.store.root, n))


def acceptance_refusal(git: GitRepo, approval: Approval, refs: Mapping[str, str]) -> str | None:
    """One line naming each changed locked path and the ref it changed on; None when all match."""
    changed = [f"{path} on {name} {ref}" for name, ref in refs.items()
               for path in acceptance_mismatches(git, approval, ref, absent_ok=False)]
    return f"approved acceptance file changed or missing: {', '.join(changed)}" if changed else None


def done_when_refusal(
    n: int, ctx: RunContext, approval: Approval, worktree: Path, log: Path,
    slot: Callable[[], AbstractContextManager],
) -> str | None:
    """Stop at the first failing approved command and quote it on one line; None when all pass.

    The base is not compared: an approved completion condition is mandatory even when it also fails there.
    """
    for command in parse_body(approval.body).done_when_commands:
        check = run_done_when(n, ctx, command, worktree, log, DONE_WHEN_SECONDS, slot=slot())
        if check.exit_code == 0 and not check.timed_out:
            continue
        outcome = (f"timed out after {DONE_WHEN_SECONDS} s" if check.timed_out
                   else f"failed (exit {check.exit_code})")
        quoted = " ".join(line.strip() for line in command.splitlines())
        return f"Done when {outcome}: {quoted}"
    return None

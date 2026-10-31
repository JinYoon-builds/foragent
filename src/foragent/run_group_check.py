"""One complete local check for consecutive ready FIFO runs on current main (group-joint).

The turn holder (the leader) takes itself plus the next live waiting entries for
the same target, up to ``merge_group_max`` including itself. It applies each
member's exact published head over the captured main SHA in queue order, with
plumbing only (``merge-tree`` + ``commit-tree``; nothing is pushed), and checks
that result in a detached temporary worktree: every member's Done when commands
from its approval snapshot, current main's passing acceptance baseline once, and one
related-unit run over the union of changed paths.

State ownership: the result is written only to the leader's own log and one
local journal event. No queue event is added and no other run's record is
written; each member still checks, publishes and finishes itself. The check runs
before the merge lock is requested, so it never holds that lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import cuts
from foragent.git import GitError
from foragent.issue import parse_body
from foragent.run_waits import _progress, _test_slot

if TYPE_CHECKING:
    from foragent.run_merge_turn import Seat
    from foragent.runner import RunContext

IDENTITY = ["-c", "user.name=Foragent", "-c", "user.email=foragent@localhost"]


@dataclass(frozen=True)
class Member:
    n: int
    head_sha: str
    commands: tuple[str, ...]


def members(ctx: RunContext, place: Seat, head_sha: str) -> list[Member]:
    """The leader, then the next live waiting entries that may join, in queue order.

    The group ends at the first live entry that cannot join: its ready head is not
    its PR's current head, or it has no approval snapshot. Dead entries are skipped.
    """
    from foragent import run_merge_turn as turn

    limit = ctx.config.merge_group_max
    leader = _member(ctx, place.n, head_sha)
    if limit <= 1 or leader is None:
        return []
    group = [leader]
    entries = turn._waiting(turn._rows(ctx.store), place.target)
    keys = list(entries)
    if place.key not in entries:
        return []
    for key in keys[keys.index(place.key) + 1:]:
        if len(group) >= limit:
            break
        if not turn._live(ctx.store, key):
            continue
        member = _waiting_member(ctx, key[0], entries[key].get("head_sha"))
        if member is None:
            break
        group.append(member)
    return group if len(group) > 1 else []


def _member(ctx: RunContext, n: int, head_sha: str) -> Member | None:
    approval = ctx.store.load_approval(n)
    if approval is None:
        return None
    return Member(n, head_sha, tuple(parse_body(approval.body).done_when_commands))


def _waiting_member(ctx: RunContext, n: int, head_sha: object) -> Member | None:
    run = ctx.store.get_run(n)
    if not isinstance(head_sha, str) or run is None or not run.branch:
        return None
    pr = ctx.tracker.find_pr(run.branch)
    if pr is None or ctx.tracker.pr_head_sha(pr) != head_sha:
        return None
    return _member(ctx, n, head_sha)


def _combine(git: GitRepo, base_sha: str, group: list[Member]) -> str:
    """Apply each member's head over ``base_sha`` in order; a conflict raises GitError."""
    commit = base_sha
    for member in group:
        try:
            git._call(["cat-file", "-e", f"{member.head_sha}^{{commit}}"])
        except GitError:
            git.fetch("origin")
        tree = git._call(["merge-tree", "--write-tree", commit, member.head_sha]).splitlines()[0].strip()
        commit = git._call([*IDENTITY, "commit-tree", tree, "-p", commit,
                            "-m", f"Foragent group check: #{member.n} at {member.head_sha}"]).strip()
    return commit


def check(
    n: int, ctx: RunContext, place: Seat, head_sha: str, base_sha: str, done: set,
) -> None:
    """Run the joint check once per (main, member heads); never stops the leader's run.

    Every entrypoint gets the same suite: the passing acceptance baseline is derived
    here from ``base_sha`` (current main; cached per SHA), never forwarded by a caller.
    """
    from foragent import orch_events, runner
    from foragent.ci_mode import enabled
    from foragent.run_attempt import AttemptFailure
    from foragent.run_baseline import _capture_acceptance_baseline
    from foragent.run_checks import _append, _pytest_command, _related_unit_tests, trusts_ci
    from foragent.run_implementation import _detect_test_command_at

    if ctx.config.merge_group_max <= 1 or enabled(ctx, place.target):
        return
    group = members(ctx, place, head_sha)
    key = (base_sha, tuple((member.n, member.head_sha) for member in group))
    if not group or key in done:
        return
    done.add(key)
    log = ctx.store.log_path(n)
    numbers = [member.n for member in group]
    _append(log, f"\nGroup check of {numbers} on {base_sha}")
    results = []
    error = None
    try:
        combined = _combine(ctx.git, base_sha, group)
        baseline = _capture_acceptance_baseline(
            n, ctx, ctx.config.test_command or _detect_test_command_at(ctx.git, base_sha), base_sha)
        with TemporaryDirectory(prefix=f"foragent-group-{n}-") as directory:
            worktree = Path(directory) / "worktree"
            ctx.git.add_detached_worktree(worktree, combined)
            try:
                def run(command: str):
                    result = runner._check(command, worktree, log, 900, progress=lambda: _progress(n, ctx),
                                           slot=_test_slot(n, ctx), full_text_dir=cuts.issue_dir(ctx.store.root, n))
                    results.append(result)
                    _progress(n, ctx)

                for member in group:
                    for command in member.commands:
                        run(command)
                if baseline is not None and baseline.node_ids:
                    run(f"{baseline.command} {shlex.join(baseline.node_ids)}")
                test_command = ctx.config.test_command
                if test_command is None:
                    test_command = next(iter(runner.detect_test_commands(worktree)), None)
                unit_command = _pytest_command(test_command) if test_command else None
                paths = [path for path in ctx.git._call(
                    ["diff", "--name-only", "-z", base_sha, combined]).split("\0") if path]
                own_only = ctx.config.runner_unit_tests != "all" and trusts_ci(worktree, ctx.config)
                units = _related_unit_tests(worktree, paths, ctx.config.acceptance_dir, own_only=own_only)
                if unit_command and units:
                    run(f"{unit_command} {shlex.join(units)}")
            finally:
                ctx.git.remove_worktree(worktree)
    except (GitError, AttemptFailure) as exc:
        if getattr(exc, "is_auth_failure", False):
            raise  # A refused credential is no check result; the run ends auth-failed (#1208).
        error = str(exc).strip() or type(exc).__name__
    passed = error is None and all(r.exit_code == 0 and not r.timed_out for r in results)
    summary = (f"group check {'passed' if passed else 'failed'}: {numbers} on {base_sha}"
               + (f" ({error})" if error else ""))
    _append(log, summary)
    orch_events.append_event(
        ctx.store, ctx.now(), n, "group-check", summary,
        members=[{"n": member.n, "head_sha": member.head_sha} for member in group], base_sha=base_sha,
        checks=[{"command": r.command, "exit_code": r.exit_code, "timed_out": r.timed_out} for r in results],
        passed=passed,
    )

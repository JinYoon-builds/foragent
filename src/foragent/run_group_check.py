"""One joint check of consecutive ready FIFO runs on captured current main.

Both modes combine exact published member heads in queue order and record the
base, heads and intermediate trees for run_group_evidence's member reuse rule.
The leader writes only its own log and group-check event, outside the merge lock.

Local mode checks every approved Done when command, current main's passing
acceptance baseline once and the union of related units in a detached worktree.
CI mode publishes the combined commit as a temporary PR, validates the ordinary
GitHub acceptance comparison and removes the temporary PR and branch afterwards.
Only a complete pass is reusable; a failure or infrastructure fault sends every
member through ordinary individual verification.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
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


def _combine(git: GitRepo, base_sha: str, group: list[Member], *, excluded=None) -> str:
    """Apply each member's head over ``base_sha`` in order; a conflict raises GitError."""
    commit = base_sha
    for index, member in enumerate(group):
        try:
            git._call(["cat-file", "-e", f"{member.head_sha}^{{commit}}"])
        except GitError:
            git.fetch("origin")
        tree = git._call(["merge-tree", "--write-tree", commit, member.head_sha]).splitlines()[0].strip()
        title = f"Foragent group check: #{member.n} at {member.head_sha}"
        if excluded is not None and index == len(group) - 1:
            from foragent.ci_workflow import EXCLUSIONS_TRAILER

            title += f"\n\n{EXCLUSIONS_TRAILER}: {json.dumps(sorted(set(excluded)), separators=(',', ':'))}"
        commit = git._call([*IDENTITY, "commit-tree", tree, "-p", commit, "-m", title]).strip()
    return commit


def check(
    n: int, ctx: RunContext, place: Seat, head_sha: str, base_sha: str, done: set,
) -> None:
    """Run the joint check once per (main, member heads); never stops the leader's run.

    Every entrypoint measures the captured base: locally against its passing
    baseline, or through exact base/head acceptance reports in CI mode.
    """
    from foragent import orch_events, runner
    from foragent.ci_mode import enabled
    from foragent.run_attempt import AttemptFailure
    from foragent.run_baseline import _capture_acceptance_baseline
    from foragent.run_checks import _append, _protected_limit, _pytest_command, _related_unit_tests, trusts_ci
    from foragent.run_implementation import _detect_test_command_at

    if ctx.config.merge_group_max <= 1:
        return
    ci = enabled(ctx, place.target)
    if ci:
        from foragent.run_group_evidence import latest

        prior = latest(ctx, n)
        # An unusable group sends its members to individual verification, not
        # another group round. A new execution may form a new group.
        if (prior is not None and prior[0].get("mode") == "ci"
                and prior[0].get("at", "") >= ctx.store.get_run(n).started_at):
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
    baseline = None
    trees: list[str] = []
    metadata = {}
    try:
        if ci:
            from foragent.run_baseline import _baseline_exclusions

            excluded = _baseline_exclusions(ctx, base_sha, log)
            combined = _combine(ctx.git, base_sha, group, excluded=excluded)
        else:
            combined = _combine(ctx.git, base_sha, group)
        trees = _trees(ctx.git, base_sha, combined)
        if ci:
            from foragent import run_group_ci

            verdict, reason, metadata = run_group_ci.check(n, ctx, place.target, base_sha, combined,
                                                         excluded=excluded)
        else:
            baseline = _capture_acceptance_baseline(
                n, ctx, ctx.config.test_command or _detect_test_command_at(ctx.git, base_sha), base_sha)
            # Without current main's passing baseline the check is incomplete: nothing to run.
            if baseline is not None and baseline.node_ids:
                with TemporaryDirectory(prefix=f"foragent-group-{n}-") as directory:
                    worktree = Path(directory) / "worktree"
                    ctx.git.add_detached_worktree(worktree, combined)
                    try:
                        def run(command: str, limit: int = 900, *, clock=None):
                            result = runner._check(command, worktree, log, limit, progress=lambda: _progress(n, ctx),
                                                   slot=_test_slot(n, ctx), clock=clock, full_text_dir=cuts.issue_dir(ctx.store.root, n))
                            results.append(result)
                            _progress(n, ctx)

                        for member in group:
                            for command in member.commands:
                                run(command)
                        run(f"{baseline.command} {shlex.join(baseline.node_ids)}",
                            _protected_limit(n, ctx, baseline), clock=ctx.monotonic)
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
    except (GitError, AttemptFailure, OSError) as exc:
        # An unreadable result is an infrastructure fault, never a pass.
        error = str(exc).strip() or type(exc).__name__
    if error is not None or not ci:
        verdict, reason = judge(error, baseline, results)
    if ci:
        metadata["mode"] = "ci"  # Including preparation errors, before a temporary PR exists.
    summary = f"group check {verdict}: {numbers} on {base_sha}" + (f" ({reason})" if reason else "")
    _append(log, summary)
    orch_events.append_event(
        ctx.store, ctx.now(), n, "group-check", summary,
        members=[{"n": member.n, "head_sha": member.head_sha,
                  **({"tree": trees[i]} if len(trees) == len(group) else {})} for i, member in enumerate(group)],
        base_sha=base_sha,
        checks=[{"command": r.command, "exit_code": r.exit_code, "timed_out": r.timed_out} for r in results],
        passed=verdict == "passed", verdict=verdict, reason=reason, **metadata,
    )


def judge(error: str | None, baseline, results: list) -> tuple[str, str | None]:
    """``passed`` only for a complete check whose every command exited exactly 0 in time.

    ``void`` (an infrastructure fault: members verify alone) for a check that could
    not run or be read, has no passing baseline, timed out or has an unknown exit
    code; ``failed`` only for a real nonzero exit.
    """
    if error is not None:
        return "void", error
    if baseline is None or not baseline.node_ids:
        return "void", "no acceptance baseline on current main"
    for result in results:
        if result.timed_out:
            return "void", f"timed out: {result.command}"
        if type(result.exit_code) is not int:
            return "void", f"unknown exit code: {result.command}"
    failing = [result.command for result in results if result.exit_code != 0]
    if failing:
        return "failed", f"failed: {failing[0]}"
    return "passed", None


def _trees(git: GitRepo, base_sha: str, combined: str) -> list[str]:
    """Each member's combined tree, in queue order: what main must hold after that member's merge."""
    lines = git._call(["log", "--first-parent", "--reverse", "--format=%T", f"{base_sha}..{combined}"])
    return [line.strip() for line in lines.splitlines() if line.strip()]

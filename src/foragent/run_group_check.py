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
import subprocess
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from foragent import cuts
from foragent.git import GitError
from foragent.issue import parse_body
from foragent.run_waits import _progress, _test_slot, in_phase

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.run_merge_turn import Seat
    from foragent.runner import RunContext
    from foragent.state import StateStore

IDENTITY = ["-c", "user.name=Foragent", "-c", "user.email=foragent@localhost"]
AUTO_START = 4
AUTO_MAX = 8
AUTO_FLOOR = 2


def group_limit(store: StateStore, config: Config) -> tuple[int, Literal["auto", "fixed"]]:
    """Derive the repository's limit from retained results, without persisting it."""
    from foragent.orch_events import events

    if isinstance(config.merge_group_max, int):
        return config.merge_group_max, "fixed"
    limit = AUTO_START
    for event in events(store, "group-check"):
        members = event.get("members")
        if not isinstance(members, list):
            continue
        if event.get("verdict") == "passed" and len(members) >= limit:
            limit = min(AUTO_MAX, limit + 1)
        elif event.get("verdict") == "failed":
            limit = max(AUTO_FLOOR, limit - 1)
    return limit, "auto"


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

    limit, _ = group_limit(ctx.store, ctx.config)
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
    """Apply the clean FIFO prefix; only merge-tree exit 1 ends it normally."""
    commit = base_sha
    for member in group:
        try:
            git._call(["cat-file", "-e", f"{member.head_sha}^{{commit}}"])
        except GitError:
            git.fetch("origin")
        try:
            tree = git._call(["merge-tree", "--write-tree", commit, member.head_sha]).splitlines()[0].strip()
        except GitError as exc:
            cause = exc.__cause__
            if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1:
                break
            raise
        title = f"Foragent group check: #{member.n} at {member.head_sha}"
        parent = commit
        commit = git._call([*IDENTITY, "commit-tree", tree, "-p", parent, "-m", title]).strip()
    if excluded is not None and commit != base_sha:
        from foragent.ci_workflow import EXCLUSIONS_TRAILER

        title += f"\n\n{EXCLUSIONS_TRAILER}: {json.dumps(sorted(set(excluded)), separators=(',', ':'))}"
        commit = git._call([*IDENTITY, "commit-tree", tree, "-p", parent, "-m", title]).strip()
    return commit


def check(
    n: int, ctx: RunContext, place: Seat, head_sha: str, base_sha: str, done: set,
) -> None:
    """Run the joint check once per (main, member heads); never stops the leader's run.

    Every entrypoint measures the captured base: locally against its passing
    baseline, or through exact base/head acceptance reports in CI mode.
    """
    from foragent.ci_mode import enabled

    if group_limit(ctx.store, ctx.config)[0] <= 1:
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
    with in_phase(n, ctx, "checking"):
        _check_group(n, ctx, place, base_sha, group, ci)


def _check_group(n: int, ctx: RunContext, place: Seat, base_sha: str, group: list[Member], ci: bool) -> None:
    """Measure the captured group while its leader holds a machine slot."""
    from foragent import orch_events, runner
    from foragent.run_attempt import AttemptFailure
    from foragent.run_baseline import _capture_acceptance_baseline
    from foragent.run_checks import _append, _protected_limit, _pytest_command, _related_unit_tests, trusts_ci
    from foragent.run_implementation import _detect_test_command_at

    log = ctx.store.log_path(n)
    numbers = [member.n for member in group]
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
        cut = (f" (cut at #{group[len(trees)].n}: merge-tree conflict)"
               if len(trees) < len(group) else "")
        group = group[:len(trees)]
        numbers = [member.n for member in group]
        _append(log, f"\nGroup check of {numbers} on {base_sha}{cut}")
        if len(group) < 2:
            return
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

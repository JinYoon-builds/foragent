"""Merge groups (#459): ready runs merge together after one joint check.

A ready run records itself in merge-queue.json, claims the oldest unclaimed
peers, checks main plus every member's head once outside merge.lock, and at
the final lock merges the still-valid members in queue order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import shlex
import tempfile
from typing import TYPE_CHECKING, Any

from foragent import board
from foragent.git import GitError, PublicationError
from foragent.issue import parse_body
from foragent.labels import READY, RUNNING
from foragent.run_checks import _append, _pytest_command, _related_unit_tests, trusts_ci
from foragent.run_waits import _progress, _test_slot
from foragent.tracker import TrackerError

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.run_attempt import Implementation
    from foragent.run_publish import _MergeInputs


@dataclass
class Group:
    base_sha: str
    entries: list[dict[str, Any]]  # Queue order, including the leader.


@dataclass
class Member:
    entry: dict[str, Any]
    git: GitRepo
    inputs: _MergeInputs


def queue_in_use(ctx) -> bool:
    """merge-queue.json is used only under local checks with groups of two or more."""
    return getattr(ctx.config, "checks", "local") == "local" and getattr(ctx.config, "merge_group_max", 1) > 1


def grouping_enabled(attempt: Implementation, automatic: bool) -> bool:
    """Only automatic merges into the configured base under local checks join groups."""
    ctx = attempt.ctx
    # The configured mode decides, not whether a CI comparison is possible.
    return (automatic and queue_in_use(ctx)
            and attempt.target_branch == ctx.config.base_branch
            and attempt.target_branch != attempt.batch_branch)


def _own_units(attempt: Implementation) -> tuple[str | None, list[str]]:
    """The related-unit command and selection this run's own verification used."""
    from foragent import runner

    ctx, worktree = attempt.ctx, attempt.worktree
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    unit_command = _pytest_command(test_command) if test_command else None
    units = _related_unit_tests(worktree, list(attempt.paths), ctx.config.acceptance_dir,
                                own_only=ctx.config.runner_unit_tests != "all" and trusts_ci(worktree, ctx.config))
    return unit_command, list(units)


def enqueue(attempt: Implementation, pr: int, head: str, base_sha: str, ready_at: float) -> None:
    baseline = attempt.baseline
    unit_command, unit_tests = _own_units(attempt)
    attempt.ctx.store.enqueue_merge({
        "number": attempt.n, "head_sha": head, "pr": pr, "branch": attempt.branch,
        "base": attempt.target_branch, "base_sha": base_sha, "ready_at": ready_at,
        "worktree": str(attempt.worktree), "body_sha": attempt.approval.body_sha,
        "acceptance_blobs": attempt.approval.acceptance_blobs,
        "baseline_command": baseline.command if baseline else None,
        "baseline_ids": list(baseline.node_ids) if baseline else [],
        "baseline_base_sha": baseline.base_sha if baseline else None,
        "baseline_test_command": (baseline.test_command or ctx_test_command(attempt)) if baseline else None,
        # The joint check keeps every member's own selection: it may only add checks.
        "unit_command": unit_command, "unit_tests": unit_tests,
    })


def ctx_test_command(attempt: Implementation) -> str | None:
    from foragent import runner

    return attempt.ctx.config.test_command or next(iter(runner.detect_test_commands(attempt.worktree)), None)


def form(attempt: Implementation) -> tuple[Group | None, bool]:
    """Claim peers and check them jointly; return (passed group, joint check failed)."""
    n, ctx = attempt.n, attempt.ctx
    entries = ctx.store.claim_merge_group(n, attempt.target_branch, ctx.config.merge_group_max)
    if not entries:
        return None, False
    members = ", ".join(f"#{entry['number']}" for entry in entries)
    try:
        # Pin the main tip when the group forms; the final lock compares against it.
        ctx.git.fetch("origin", attempt.target_branch)
        base_sha = ctx.git.ref_sha(f"origin/{attempt.target_branch}")
        _append(attempt.log, f"Merge group {members}: joint check on {base_sha}")
        try:
            passed, entries = joint_check(attempt, entries, base_sha)
        except GitError:
            if ctx.store.group_merged_head(n) is None:
                raise
            return None, False  # Another group merged this run meanwhile; the caller finishes it.
    finally:
        # Claims end after the joint check; the final lock re-reads every member.
        ctx.store.release_merge_claims(n)
    if len(entries) < 2:
        return None, False  # Every peer went stale before the check: proceed alone.
    members = ", ".join(f"#{entry['number']}" for entry in entries)
    _append(attempt.log, f"Merge group {members}: joint check {'passed' if passed else 'failed'}")
    return (Group(base_sha, entries), False) if passed else (None, True)


def _stale(attempt: Implementation, entry: dict[str, Any]) -> bool:
    """A queued peer that stopped, restarted or lost its worktree since it queued."""
    return (entry["number"] != attempt.n
            and (not attempt.ctx.store.merge_entry_current(entry) or not Path(entry["worktree"]).is_dir()))


def joint_check(attempt: Implementation, entries: list[dict[str, Any]],
                base_sha: str) -> tuple[bool, list[dict[str, Any]]]:
    """Main plus every member's head in queue order, with the union of their checks.

    Returns (passed, checked members). A peer that went stale (for example it merged
    alone and its branch was deleted) is dropped before use instead of failing the check.
    """
    from foragent import runner

    n, ctx, log = attempt.n, attempt.ctx, attempt.log
    with tempfile.TemporaryDirectory(prefix=f"foragent-group-{n}-", dir=ctx.store.root) as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, base_sha)
        try:
            git = ctx.git.at(worktree)
            checked = []
            for entry in entries:
                try:
                    if _stale(attempt, entry):
                        raise GitError("its run is no longer the queued execution")
                    ctx.git.fetch("origin", entry["branch"])
                except GitError as exc:
                    if entry["number"] == attempt.n:
                        raise
                    _append(log, f"Merge group: #{entry['number']} dropped as stale: {exc}")
                    ctx.store.dequeue_merge(entry["number"])  # Never select it again.
                    continue
                checked.append(entry)
            entries = checked
            if len(entries) < 2:
                return False, entries
            for entry in entries:
                try:
                    git.merge_base(entry["head_sha"])
                except GitError as exc:
                    _append(log, f"Merge group: #{entry['number']} does not merge cleanly: {exc}")
                    return False, entries
            # The checks must verify exactly the contents that will be merged.
            combined_head = git.head_sha()
            git.stage_changes()
            combined_tree = git.staged_tree_sha()

            def check(command: str, timeout_s: int = 900):
                result = runner._check(command, worktree, log, timeout_s, progress=lambda: _progress(n, ctx),
                                       slot=_test_slot(n, ctx))
                _progress(n, ctx)
                return result

            checks = []
            for entry in entries:
                approval = ctx.store.load_approval(entry["number"])
                if approval is None:
                    return False, entries
                checks.extend(check(command) for command in parse_body(approval.body).done_when_commands)
            command = next((entry["baseline_command"] for entry in entries if entry.get("baseline_command")), None)
            ids = {node for entry in entries for node in entry.get("baseline_ids") or ()}
            # Members' baselines ran on their own bases. When main moved since (for
            # example another group merged the owner of a pending acceptance test),
            # the tests that pass on this pinned main must be protected too.
            fresh = _baseline_on(attempt, entries, base_sha)
            if fresh is None:
                _append(log, "Merge group: no acceptance baseline on the pinned main; merging one at a time")
                return False, entries
            ids |= fresh
            if command and ids:
                checks.append(check(f"{command} {shlex.join(sorted(ids))}"))
            for unit_command, units in _unit_runs(attempt, entries, worktree, git, base_sha).items():
                checks.append(check(f"{unit_command} {shlex.join(units)}", max(900, runner._remaining(n, ctx))))
            git.stage_changes()
            if git.head_sha() != combined_head or git.staged_tree_sha() != combined_tree:
                _append(log, "Merge group: joint check commands modified tracked files")
                return False, entries
            return all(result.exit_code == 0 and not result.timed_out for result in checks), entries
        finally:
            ctx.git.remove_worktree(worktree)


def _baseline_on(attempt: Implementation, entries: list[dict[str, Any]], base_sha: str) -> set[str] | None:
    """Pass IDs of a fresh acceptance baseline on the pinned main, when any member's is older.

    None means it could not be captured, so the group cannot be protected.
    """
    from foragent.run_baseline import _capture_acceptance_baseline

    if all(entry.get("baseline_base_sha") in (None, base_sha) for entry in entries):
        return set()
    test_command = next((entry["baseline_test_command"] for entry in entries
                         if entry.get("baseline_test_command")), None)
    if test_command is None:
        return set()
    try:
        baseline = _capture_acceptance_baseline(attempt.n, attempt.ctx, test_command, base_sha)
    except Exception as exc:  # noqa: BLE001 - an unprotected group falls back to one at a time
        _append(attempt.log, f"Merge group: acceptance baseline on {base_sha} failed: {exc}")
        return None
    return set(baseline.node_ids) if baseline else set()


def _unit_runs(attempt: Implementation, entries: list[dict[str, Any]], worktree: Path,
               git: GitRepo, base_sha: str) -> dict[str, list[str]]:
    """Related units once per command: every member's own selection plus the combined one."""
    from foragent import runner

    ctx = attempt.ctx
    runs: dict[str, set[str]] = {}
    for entry in entries:
        if entry.get("unit_command") and entry.get("unit_tests"):
            runs.setdefault(entry["unit_command"], set()).update(entry["unit_tests"])
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    combined = _pytest_command(test_command) if test_command else None
    units = _related_unit_tests(worktree, git.changed_paths(base_sha), ctx.config.acceptance_dir,
                                own_only=ctx.config.runner_unit_tests != "all" and trusts_ci(worktree, ctx.config))
    if combined and units:
        runs.setdefault(combined, set()).update(units)
    # A member's own test file that a later member deleted no longer exists here.
    return {command: sorted(path for path in tests if (worktree / path).is_file())
            for command, tests in runs.items() if any((worktree / path).is_file() for path in tests)}


def merged_by_group(attempt: Implementation, pr: int, head: str) -> bool:
    """Another run's group already merged this exact head (run record and PR)."""
    ctx = attempt.ctx
    if ctx.store.group_merged_head(attempt.n) != head:
        return False
    return any(item.number == pr for item in ctx.tracker.list_prs("merged", attempt.branch))


def member_inputs(attempt: Implementation, group: Group, base_sha: str) -> list[Member]:
    """Re-read every borrowed member's merge inputs; invalid members are left out.

    The caller merges borrowed members only when every member is still valid:
    the joint PASS covers the whole combination, not any subset of it.
    """
    from foragent.run_publish import _final_merge_inputs, _policy_allows

    ctx = attempt.ctx
    members = []
    for entry in group.entries:
        number = entry["number"]
        if number == attempt.n:
            continue
        saved = ctx.store.load_approval(number)
        run = ctx.store.get_run(number)
        # Only the execution that queued this entry may be merged by it.
        if saved is None or run is None or run.started_at != entry.get("started_at") or _stale(attempt, entry):
            ctx.store.dequeue_merge(number)  # Stale: never select it again.
            continue
        approval = replace(saved, body_sha=entry["body_sha"], acceptance_blobs=entry["acceptance_blobs"])
        git = ctx.git.at(Path(entry["worktree"]))
        try:
            inputs = _final_merge_inputs(number, ctx, git, entry["pr"], entry["branch"], entry["base"],
                                         entry["head_sha"], approval, entry["ready_at"], base_sha=base_sha)
        except GitError as exc:
            _append(attempt.log, f"Merge group: #{number} left out as stale: {exc}")
            ctx.store.dequeue_merge(number)
            continue
        if (inputs.stop is None and inputs.status in {"pass", "none"}
                and _policy_allows(inputs.decision, entry["base"], inputs.batch_branch)
                and inputs.decision.kind == "merge"):
            try:
                git.check_publication(base_sha)
            except (PublicationError, GitError) as exc:
                _append(attempt.log, f"Merge group: #{number} left out: {exc}")
                continue
            members.append(Member(entry, git, inputs))
    return members


def merge_member(attempt: Implementation, member: Member) -> bool:
    """Merge one borrowed member with its expected head; False leaves it out."""
    from foragent.run_publish import retitle_for_merge

    ctx, entry = attempt.ctx, member.entry
    number, pr = entry["number"], entry["pr"]
    current = next((item for item in ctx.tracker.list_prs("open", entry["branch"]) if item.number == pr), None)
    title = f"{ctx.tracker.get_issue(number).title} (#{number})"
    subject = retitle_for_merge(
        ctx, pr, title=title, current_title=current.title if current else None, base=entry["base"],
        body=lambda: current.body if current else "", note=lambda line: _append(attempt.log, line),
    )
    try:
        ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=entry["head_sha"], **subject)
    except TrackerError as exc:
        if exc.is_rate_limit:
            raise
        _append(attempt.log, f"Merge group: #{number} merge failed: {exc}")
        return False
    # Durable at once: the member's own run finishes merged even if the leader stops here.
    ctx.store.record_merge(number, ctx.now())
    ctx.store.record_group_merge(number, entry["head_sha"], entry.get("started_at"))
    return True


def merge_in_order(
    attempt: Implementation, order: list[dict[str, Any]], members: dict[int, Member],
    merge_leader: Callable[[], bool],
) -> tuple[list[dict[str, Any]], bool, Exception | None]:
    """Merge in queue order; return (borrowed merged, leader merged, error) without raising.

    The caller finishes every merge that happened, then reports the error. A member
    that fails stops the rest: they were checked only together with it.
    """
    borrowed: list[dict[str, Any]] = []
    leader_merged = False
    try:
        for entry in order:
            if entry["number"] != attempt.n:
                if not merge_member(attempt, members[entry["number"]]):
                    break
                borrowed.append(entry)
            elif merge_leader():
                leader_merged = True
            else:
                break
    except Exception as exc:  # noqa: BLE001 - returned so that merged PRs are finished first
        return borrowed, leader_merged, exc
    return borrowed, leader_merged, None


def finish_group(attempt: Implementation, borrowed: list[dict[str, Any]], leader: tuple[int, str] | None,
                 *, reinstall: bool, queue: bool) -> None:
    """Finish every merge of a group, local facts first and fallible GitHub work last.

    Durable local state (group fields, queue, reinstall, journal) never waits for
    GitHub, and each member's closure is attempted even after another one failed;
    a failed closure is queued for the outbox retry (close_merged). Never raises
    for GitHub errors, so a caller always finalizes every merge that happened.
    """
    from foragent import runner
    from foragent.orch_events import append_event

    ctx, n = attempt.ctx, attempt.n
    merged = [(entry["number"], entry["pr"], entry["head_sha"]) for entry in borrowed]
    if leader is not None:
        merged.append((n, *leader))
    size = len(merged)
    if not size:
        return
    numbers = [number for number, _, _ in merged]
    for number in numbers:
        ctx.store.update_run(number, merge_group_size=size, merge_group_bisect_checks=0)
    if queue:
        ctx.store.dequeue_merge(*numbers)
    if reinstall:  # Once per group, after its last merge.
        runner.after_merge(ctx, merged[-1][1], attempt.target_branch)
    if size > 1:
        for number, pr, head in merged:
            others = ", ".join(f"#{other}" for other in numbers if other != number)
            append_event(ctx.store, ctx.now(), number, "merge",
                         f"PR #{pr}: merged {head} in a merge group with {others}")
    for number in numbers:
        if number == n:
            continue  # The leader closes its own issue after this.
        others = ", ".join(f"#{other}" for other in numbers if other != number)
        close_merged(attempt, number, f"Merged in a merge group with {others}.")


def _close_merged(ctx, number: int, comment: str | None) -> None:
    """Close a merged issue and clear its labels; each step runs even if an earlier one failed."""
    errors: list[BaseException] = []

    def close() -> None:
        if ctx.tracker.current_issue(number).state == "closed":
            return
        try:
            if comment is None:
                ctx.tracker.close_issue(number)
            else:
                ctx.tracker.close_issue(number, comment)
        except TrackerError as exc:
            if exc.is_rate_limit or ctx.tracker.current_issue(number).state != "closed":
                raise

    for step in (close, lambda: ctx.tracker.remove_labels(number, [READY, RUNNING]),
                 lambda: board.sync_status(ctx, number, "Done")):
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    if errors:
        raise errors[0]


def close_merged(attempt: Implementation, number: int, comment: str | None) -> bool:
    """Close a merged issue, or queue the closure for the gate's outbox retry.

    A failure is never silent: the closure and label removal wait in the GitHub
    outbox (delivered by the next tick), and the journal and run record say so.
    Returns whether the issue was finished now.
    """
    from foragent.orch_events import append_event

    ctx = attempt.ctx
    try:
        _close_merged(ctx, number, comment)
        return True
    except Exception as exc:  # noqa: BLE001 - the merge happened; retry the closure instead
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            from foragent.orch_github import pause

            pause(ctx, exc)  # The gate waits for the reset, then the outbox delivers.
        lanes = getattr(getattr(ctx, "config", None), "lanes", None)
        ctx.store.queue_github_write(number, "close_issue", lanes=lanes, body=comment)
        ctx.store.queue_github_write(number, "remove_labels", lanes=lanes, labels=[READY, RUNNING])
        detail = f"Merged; closing the issue failed and is queued for retry: {exc}"
        _append(attempt.log, f"#{number}: {detail}")
        append_event(ctx.store, ctx.now(), number, "close-pending", detail)
        run = ctx.store.get_run(number)
        if run is not None and run.status == "running":
            ctx.store.update_run(number, detail=detail)
        return False


def finish_merged_self(attempt: Implementation) -> bool:
    """A member merged by another group closes its own issue if the leader could not."""
    return close_merged(attempt, attempt.n, "Merged in another run's merge group.")

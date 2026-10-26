"""Merge groups (#459): ready runs merge together after one joint check.

A ready run records itself in merge-queue.json, claims the oldest unclaimed
peers, checks main plus every member's head once outside merge.lock, and at
the final lock merges the still-valid members in queue order.
"""

from __future__ import annotations

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


def grouping_enabled(attempt: Implementation, automatic: bool) -> bool:
    """Only automatic merges into the configured base under local checks join groups."""
    ctx = attempt.ctx
    return (automatic and getattr(ctx.config, "merge_group_max", 1) > 1
            and attempt.target_branch == ctx.config.base_branch
            and attempt.target_branch != attempt.batch_branch
            # The configured mode decides, not whether a CI comparison is possible.
            and getattr(ctx.config, "checks", "local") == "local")


def enqueue(attempt: Implementation, pr: int, head: str, base_sha: str, ready_at: float) -> None:
    baseline = attempt.baseline
    attempt.ctx.store.enqueue_merge({
        "number": attempt.n, "head_sha": head, "pr": pr, "branch": attempt.branch,
        "base": attempt.target_branch, "base_sha": base_sha, "ready_at": ready_at,
        "worktree": str(attempt.worktree), "body_sha": attempt.approval.body_sha,
        "acceptance_blobs": attempt.approval.acceptance_blobs,
        "baseline_command": baseline.command if baseline else None,
        "baseline_ids": list(baseline.node_ids) if baseline else [],
    })


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
        passed = joint_check(attempt, entries, base_sha)
    finally:
        # Claims end after the joint check; the final lock re-reads every member.
        ctx.store.release_merge_claims(n)
    _append(attempt.log, f"Merge group {members}: joint check {'passed' if passed else 'failed'}")
    return (Group(base_sha, entries), False) if passed else (None, True)


def joint_check(attempt: Implementation, entries: list[dict[str, Any]], base_sha: str) -> bool:
    """Main plus every member's head in queue order, with the union of their checks."""
    from foragent import runner

    n, ctx, log = attempt.n, attempt.ctx, attempt.log
    with tempfile.TemporaryDirectory(prefix=f"foragent-group-{n}-", dir=ctx.store.root) as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, base_sha)
        try:
            git = ctx.git.at(worktree)
            for entry in entries:
                ctx.git.fetch("origin", entry["branch"])
                try:
                    git.merge_base(entry["head_sha"])
                except GitError as exc:
                    _append(log, f"Merge group: #{entry['number']} does not merge cleanly: {exc}")
                    return False
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
                    return False
                checks.extend(check(command) for command in parse_body(approval.body).done_when_commands)
            command = next((entry["baseline_command"] for entry in entries if entry.get("baseline_command")), None)
            ids = sorted({node for entry in entries for node in entry.get("baseline_ids") or ()})
            if command and ids:
                checks.append(check(f"{command} {shlex.join(ids)}"))
            test_command = ctx.config.test_command
            if test_command is None:
                test_command = next(iter(runner.detect_test_commands(worktree)), None)
            unit_command = _pytest_command(test_command) if test_command else None
            units = _related_unit_tests(
                worktree, git.changed_paths(base_sha), ctx.config.acceptance_dir,
                own_only=ctx.config.runner_unit_tests != "all" and trusts_ci(worktree, ctx.config))
            if units and unit_command:
                checks.append(check(f"{unit_command} {shlex.join(units)}", max(900, runner._remaining(n, ctx))))
            git.stage_changes()
            if git.head_sha() != combined_head or git.staged_tree_sha() != combined_tree:
                _append(log, "Merge group: joint check commands modified tracked files")
                return False
            return all(result.exit_code == 0 and not result.timed_out for result in checks)
        finally:
            ctx.git.remove_worktree(worktree)


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
        if (saved is None or run is None or run.status != "running" or run.started_at != entry.get("started_at")
                or not Path(entry["worktree"]).is_dir()):
            continue
        approval = replace(saved, body_sha=entry["body_sha"], acceptance_blobs=entry["acceptance_blobs"])
        git = ctx.git.at(Path(entry["worktree"]))
        inputs = _final_merge_inputs(number, ctx, git, entry["pr"], entry["branch"], entry["base"],
                                     entry["head_sha"], approval, entry["ready_at"], base_sha=base_sha)
        if (inputs.stop is None and inputs.status in {"pass", "none"}
                and _policy_allows(inputs.decision, entry["base"], inputs.batch_branch)
                and inputs.decision.kind == "merge"):
            try:
                git.check_publication(base_sha)
            except PublicationError as exc:
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


def finish_group(attempt: Implementation, borrowed: list[dict[str, Any]], leader: tuple[int, str] | None,
                 *, reinstall: bool, raise_errors: bool = False) -> None:
    """Finish every merge of a group, local facts first and fallible GitHub work last.

    Durable local state (group fields, queue, reinstall, journal) never waits for
    GitHub; each member's GitHub finalization is attempted even after another one
    failed, and the first error is raised at the end. A borrowed member's own run
    closes its issue when this could not (finish_merged_self).
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
    ctx.store.dequeue_merge(*numbers)
    if reinstall:  # Once per group, after its last merge.
        runner.after_merge(ctx, merged[-1][1], attempt.target_branch)
    if size > 1:
        for number, pr, head in merged:
            others = ", ".join(f"#{other}" for other in numbers if other != number)
            append_event(ctx.store, ctx.now(), number, "merge",
                         f"PR #{pr}: merged {head} in a merge group with {others}")
    errors: list[BaseException] = []
    for number in numbers:
        if number == n:
            continue  # The leader closes its own issue after this.
        others = ", ".join(f"#{other}" for other in numbers if other != number)
        try:
            _close_merged(ctx, number, f"Merged in a merge group with {others}.")
        except Exception as exc:  # noqa: BLE001 - every member is attempted; the first error is raised
            _append(attempt.log, f"Merge group: finishing #{number} failed: {exc}")
            errors.append(exc)
    if errors and raise_errors:
        raise errors[0]


def _close_merged(ctx, number: int, comment: str) -> None:
    """Close a merged issue and clear its labels; each step runs even if an earlier one failed."""
    errors: list[BaseException] = []

    def close() -> None:
        if ctx.tracker.current_issue(number).state == "closed":
            return
        try:
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


def finish_merged_self(attempt: Implementation) -> None:
    """A member merged by another group closes its own issue if the leader could not."""
    _close_merged(attempt.ctx, attempt.n, "Merged in another run's merge group.")

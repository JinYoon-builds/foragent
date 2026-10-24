"""Execute, independently check and review one approved snapshot."""

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import islice
import ast
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET

import yaml

from foragent import board, capacity, orch_github
from foragent.actions import FlagContext, flag, invalidate_approval
from foragent.config import Config, resolved_reviewer
from foragent.executors import ExecResult, Executor, classify_result, executor_for, stream_session_id
from foragent.git import GitError, GitRepo, PublicationError, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue, body_hash, has_machine_check, parse_body
from foragent.kick import kick_tick
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, attempt_tier, issue_lane, issue_size
from foragent.order import detect_test_commands
from foragent.orch_reinstall import after_merge
from foragent.policy import merge_decision, review_mode_for, touched
from foragent.processes import observe_tree, run_logged, track_process_group
from foragent.reasons import Reason
from foragent.skills import SkillManager, copy_lane_skills
from foragent.state import Approval, StateStore
from foragent.tracker import FakeTracker, RecordingTracker, Tracker, TrackerError


# Preserve the original facade bindings for callers and monkeypatches.
from foragent.run_prompts import (
    REVIEW_PROMPT,
    review_scope_rules,
    build_review_prompt,
    goal_condition,
    build_prompt,
    build_resume_prompt,
)
from foragent.run_waits import (
    poll_executor,
    _remaining,
    _timeout,
    _test_slot,
    _progress,
    _phase,
    wait_for_rate_limit,
    _executor_wait,
    _credited_wait,
)
from foragent.run_lane_skills import (
    _lane_notes,
    _lane_skill_names,
    _inject_lane_skills,
    _lane_skills,
)
from foragent.run_execution import (
    _claude_run_dir,
    _write_claude_settings,
    _run_executor,
    _available_executor,
)
from foragent.run_baseline import (
    AcceptanceBaseline,
    _baseline_environment,
    _capture_acceptance_baseline,
)
from foragent.run_checks import (
    CheckResult,
    _append,
    _check,
    _pytest_command,
    _pytest_invocation,
    run_tests,
    junit_node_id,
    _passing_acceptance_ids,
    has_pull_request_ci,
    trusts_ci,
    _related_unit_tests,
    check_done,
    _acceptance_matches,
)
from foragent.run_review import (
    ReviewResult,
    parse_review,
    parse_code_review,
    _claude_review_scope,
    _review,
    _read_rebuttals,
    _next_review_round,
    _normalized_findings,
    _review_only,
)
from foragent.run_publish import (
    _pr_body,
    _hold_changed_head,
    _ci_failure,
    _merge_lock,
    _batch_pr,
    _check_publication,
    _preserve_failed_implementation,
)
from foragent.run_attempt import (
    PatchReplayError,
    AttemptFailure,
    ConflictBudgetExhausted,
    CIFailure,
    ExecutorStopped,
)
from foragent.run_checks import _verify_implementation
from foragent.run_conflicts import _block_rebuild, _rebuild, _recover_replay, _resume_implementation
from foragent.run_implementation import _implement, _prepare_implementation
from foragent.run_publish import _publish, _push_branch
from foragent.run_review import _review_implementation


@dataclass
class RunContext:
    repo: str
    repo_root: Path
    tracker: Tracker
    store: StateStore
    config: Config
    git: GitRepo
    now: Callable[[], datetime]
    executor_factory: Callable[[str], Executor] = executor_for
    sleep: Callable[[float], None] = time.sleep
    poll_hook: Callable[[Callable[[], ExecResult], Callable[[], None]], ExecResult] = poll_executor
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.tracker = RecordingTracker(self.tracker, self)


def _execute(n: int, ctx: RunContext, worktree: Path) -> str:
    approval = ctx.store.load_approval(n)
    if approval is None:
        ctx.store.update_run(n, detail="No approved snapshot is available.")
        ctx.tracker.remove_labels(n, [RUNNING])
        return "no-approval"

    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    target_branch = ctx.config.base_branch
    ctx.git.remove_worktree(worktree)
    ctx.git.fetch("origin", ctx.config.base_branch)
    if not _acceptance_matches(n, ctx, ctx.git, approval, base_ref):
        return "hash-mismatch"
    if ctx.config.review == "batch" and batch_branch:
        ctx.git.ensure_batch_branch(batch_branch, base_ref)
        target_branch = batch_branch
    start_ref = f"origin/{target_branch}"
    if saved_head := ctx.git.local_branch_sha(branch):
        start_ref = saved_head
    elif ctx.git.remote_branch_sha(branch) is not None:
        ctx.git.fetch("origin", branch)
        start_ref = f"origin/{branch}"
    ctx.git.add_worktree(worktree, branch, start_ref)
    git = ctx.git.at(worktree)
    _inject_lane_skills(ctx, n, git, f"origin/{target_branch}")
    _progress(n, ctx)

    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    return _implement_and_publish(n, ctx, git, target_branch=target_branch)


def _implement_and_publish(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
) -> str:
    """Run named implementation, check, review and publication steps."""
    attempt = _prepare_implementation(n, ctx, git, target_branch=target_branch, failure=failure)
    outcome = "error"
    try:
        resume_verified = _resume_implementation(attempt)
        if not _acceptance_matches(n, ctx, git, attempt.approval):
            raise ExecutorStopped("hash-mismatch")
        attempt.approved_ref = git.head_sha()
        while True:
            if resume_verified:
                attempt.failure = ""
                resume_verified = False
            else:
                _implement(attempt, attempt.failure, reviewer_text=attempt.reviewer_text)
                try:
                    attempt.failure = _verify_implementation(attempt)
                except PatchReplayError as exc:
                    attempt.failure = _recover_replay(attempt, exc)
            attempt.reviewer_text = None
            run = ctx.store.get_run(n)
            if not attempt.failure and ctx.config.reviewer != "none":
                if _remaining(n, ctx) <= 0:
                    outcome = _timeout(n, ctx)
                    return outcome
                _review_implementation(attempt)
            if attempt.rebuilt and attempt.failure:
                outcome = _block_rebuild(attempt, str(attempt.failure))
                return outcome
            if not attempt.failure or attempt.review_hold:
                try:
                    outcome = _publish(attempt)
                    return outcome
                except ConflictBudgetExhausted as exc:
                    attempt.failure = _rebuild(attempt, exc)
                    continue
                except CIFailure as exc:
                    if attempt.rebuilt:
                        outcome = _block_rebuild(attempt, str(exc))
                        return outcome
                    attempt.failure = exc
                    continue
                except (AttemptFailure, PatchReplayError) as exc:
                    attempt.failure = _recover_replay(attempt, exc) if isinstance(exc, PatchReplayError) else str(exc)
                    run = ctx.store.get_run(n)
            if attempt.rebuilt:
                outcome = _block_rebuild(attempt, str(attempt.failure))
                return outcome
            _append(attempt.log, f"Attempt {run.attempts} failed:\n{attempt.failure}")
            ctx.store.update_run(n, resume_reason=str(attempt.failure))
            if run.attempts < ctx.config.max_retries + 1:
                ctx.store.update_run(n, attempts=run.attempts + 1)
                continue
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", attempt.failure)
            outcome = "retries-exceeded"
            return outcome
    except ExecutorStopped as exc:
        outcome = exc.outcome
        raise
    except TrackerError as exc:
        attempt.tracker_available = False
        outcome = "limit-wait" if exc.is_rate_limit else "error"
        raise
    except (SystemExit, KeyboardInterrupt):
        outcome = "killed"
        raise
    finally:
        if outcome != "merged":
            run = ctx.store.get_run(n)
            _preserve_failed_implementation(
                n, ctx, git, run.outcome or outcome, approval=attempt.approval, approved_ref=attempt.approved_ref,
                title=attempt.issue.title, pushed_head=attempt.pushed_head, tracker_available=attempt.tracker_available,
                push_branch=lambda: _push_branch(attempt),
            )


def review_only(
    n: int, ctx: RunContext, *, verdict_head: str | None = None, repair_ci: bool = True,
) -> str:
    """Review a PR, optionally repairing CI inline instead of returning its failure."""
    worktree = ctx.store.worktree_path(n)
    outcome = "error"
    detail = ""
    previous = ctx.store.get_run(n)
    started = False
    try:
        if previous is not None and previous.status == "running":
            raise TrackerError(f"issue #{n} already has a running execution")
        if until := ctx.store.github_wait_until(ctx.now()):
            ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree,
                                lane=previous.lane if previous is not None else "main")
            started = True
            outcome = orch_github.wait_run(ctx, n, until)
            return outcome
        issue = ctx.tracker.get_issue(n)
        if issue.state != "open" or issue.is_pull_request:
            raise ValueError(f"#{n} must be an open issue")
        if RUNNING in issue.labels:
            raise TrackerError(f"issue #{n} already has a running execution")
        ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree, pid=os.getpid(),
                            lane=issue_lane(issue.labels, ctx.config.lanes))
        started = True
        ctx.tracker.add_labels(n, [RUNNING])
        ctx.tracker.remove_labels(n, [BLOCKED, NEEDS_HUMAN, READY])
        ctx.git.remove_worktree(worktree)
        try:
            outcome = _review_only(n, ctx, worktree, verdict_head)
        except AttemptFailure as exc:
            if not repair_ci:
                # Let the caller schedule repair without changing the verdict's head.
                raise ExecutorStopped("ci-failed", str(exc)) from exc
            git = ctx.git.at(worktree)
            git.attach_branch(f"foragent/issue-{n}")
            _inject_lane_skills(ctx, n, git, f"origin/{exc.target_branch}")
            outcome = _implement_and_publish(n, ctx, git, target_branch=exc.target_branch, failure=exc)
    except ExecutorStopped as exc:
        outcome = exc.outcome
        detail = str(exc)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        if not started:
            if not isinstance(exc, TrackerError) or not exc.is_rate_limit:
                raise
            ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree,
                                lane=previous.lane if previous is not None else "main")
            started = True
        _append(ctx.store.log_path(n), traceback.format_exc())
        if isinstance(exc, TrackerError) and wait_for_rate_limit(n, ctx, exc):
            outcome = "limit-wait"
        else:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Review runner failed: {' '.join(str(exc).split())}")
    except SystemExit as exc:
        outcome = "killed"
        detail = f"Review runner exited: {exc}"
        raise
    finally:
        if started:
            try:
                ctx.git.remove_worktree(worktree)
            finally:
                try:
                    # A rate-limit wait is discoverable locally; defer label repair.
                    if outcome != "limit-wait":
                        current = ctx.tracker.current_issue(n)
                        if current.state == "open" and not current.labels & {BLOCKED, READY}:
                            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Review did not finish; retry or rerun review.")
                        ctx.tracker.remove_labels(n, [RUNNING])
                except TrackerError as exc:
                    if not wait_for_rate_limit(n, ctx, exc):
                        raise
                    detail = str(exc) or type(exc).__name__
                    outcome = "limit-wait"
                finally:
                    try:
                        ctx.store.finish_run(n, outcome=outcome, detail=detail)
                    finally:
                        kick_tick(ctx.repo_root)
    return outcome


def run_issue(n: int, ctx: RunContext) -> str:
    outcome = "error"
    detail = ""
    worktree = ctx.store.worktree_path(n)
    log = ctx.store.log_path(n)
    try:
        run = ctx.store.update_run(n, pid=os.getpid(),
                                   process_identities=observe_tree(roots=(os.getpid(),)))
        if until := ctx.store.github_wait_until(ctx.now()):
            outcome = orch_github.wait_run(ctx, n, until)
            return outcome
        board.sync_status(ctx, n, "Running")
        from foragent.run_implementation import lane_config

        ctx = replace(ctx, config=lane_config(ctx.config, run.lane))
        try:
            outcome = _execute(n, ctx, worktree)
        finally:
            ctx.git.remove_worktree(worktree)
    except ExecutorStopped as exc:
        outcome = exc.outcome
        detail = str(exc)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        _append(log, traceback.format_exc())
        if isinstance(exc, TrackerError) and wait_for_rate_limit(n, ctx, exc):
            outcome = "limit-wait"
        else:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Runner failed: {' '.join(str(exc).split())}")
            outcome = "error"
    except SystemExit as exc:
        outcome = "killed"
        detail = f"Runner exited: {exc}"
        # A handled stop must not look like an orphan to the next tick.
        ctx.tracker.add_labels(n, [BLOCKED])
        ctx.tracker.remove_labels(n, [READY, RUNNING])
        raise
    finally:
        # A tick may already have finished the record while terminating this runner.
        try:
            run = ctx.store.get_run(n)
            if run is not None and run.status != "finished":
                ctx.store.finish_run(n, outcome=run.outcome or outcome, detail=detail)
        finally:
            kick_tick(ctx.repo_root)
    return outcome


SMOKE_FILE = "hello.txt"
SMOKE_CHECK = f'test "$(cat {SMOKE_FILE})" = ok'


@dataclass(frozen=True)
class LiveCheck:
    executor: str
    passed: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'} {self.executor}: {self.detail}"


def _one_line(text: str) -> str:
    return " / ".join(line.strip() for line in text.strip().splitlines() if line.strip())


DOCTOR_ISSUE = 1
DOCTOR_BODY = f"""## Goal
Doctor smoke task: create the file {SMOKE_FILE} at the repository root containing exactly the line `ok`.

## Out of scope
Changing any other file.

## Done when
```sh
{SMOKE_CHECK}
```
"""


class _RecordedExecutor:
    """Keep the real adapter's last result so the doctor can quote the executor itself."""

    def __init__(self, executor: Executor):
        self.executor = executor
        self.result: ExecResult | None = None

    def run(self, **kwargs) -> ExecResult:
        self.result = self.executor.run(**kwargs)
        return self.result


def live_check(repo_root: Path, config: Config, executor_name: str, *, timeout_s: int = 900) -> LiveCheck:
    """Run one executor through the issue runner's implementation step on a throwaway clone.

    The approval snapshot, run record and tracker are local and temporary, so the
    runner's own implementer setup (lane-adjusted model and tier, Claude settings
    with the check-done Stop hook, /goal, availability handling) runs unchanged
    without touching GitHub, the user's checkout or the project's state.
    """
    from foragent.run_implementation import _implement, _prepare_implementation

    if shutil.which(executor_name) is None:
        return LiveCheck(executor_name, False, f"`{executor_name}` executable not found on PATH")
    config = replace(
        config, executor=executor_name, board=None,
        max_runtime_minutes=max(1, min(config.max_runtime_minutes, timeout_s // 60)),
        # A doctor reports unavailability instead of waiting it out.
        transient_max_wait_minutes=0,
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="foragent-doctor-") as temporary:
        clone = Path(temporary) / "repo"
        # A clone, unlike a worktree, leaves the user's repository metadata untouched.
        cloned = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(repo_root), str(clone)],
            capture_output=True, text=True,
        )
        if cloned.returncode != 0:
            return LiveCheck(executor_name, False, "temporary clone failed: " + _one_line(cloned.stderr))
        # Without a remote, nothing the executor does can reach the original repository.
        subprocess.run(["git", "-C", str(clone), "remote", "remove", "origin"], capture_output=True)
        # A committed artifact must not let an executor that did nothing pass the check.
        (clone / SMOKE_FILE).unlink(missing_ok=True)
        store = StateStore(Path(temporary) / "state")
        now = lambda: datetime.now(UTC)  # noqa: E731
        store.save_approval(
            DOCTOR_ISSUE, body=DOCTOR_BODY, body_sha=body_hash(DOCTOR_BODY), acceptance_blobs={},
            approved_by="fa-doctor", approved_at=now().isoformat(),
        )
        tracker = FakeTracker(now=now)
        tracker.add_issue(DOCTOR_ISSUE, "fa doctor --live smoke task", DOCTOR_BODY, [APPROVED, RUNNING])
        store.start_run(DOCTOR_ISSUE, branch="foragent/doctor", worktree=clone)
        recorded = _RecordedExecutor(executor_for(executor_name))
        ctx = RunContext(
            repo=config.repo or "local/doctor", repo_root=clone, tracker=tracker, store=store,
            config=config, git=GitRepo(clone), now=now, executor_factory=lambda name: recorded,
        )
        stopped = ""
        try:
            attempt = _prepare_implementation(
                DOCTOR_ISSUE, ctx, ctx.git, target_branch=config.base_branch, capture_baseline=False,
            )
            _implement(attempt, "")
        except ExecutorStopped as exc:
            stopped = f"runner stopped ({exc.outcome})"
        except OSError as exc:
            return LiveCheck(executor_name, False,
                             f"`{executor_name}` could not start (not found or not executable): {exc}")
        seconds = time.monotonic() - started
        result = recorded.result
        log = store.log_path(DOCTOR_ISSUE)
        tail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.is_file() else ""
        said = _one_line((result.last_message or result.log_tail if result else "") or tail)
        turns = "unknown turns" if result is None or result.num_turns is None else (
            f"{result.num_turns} turn" + ("" if result.num_turns == 1 else "s"))
        stats = f"{turns}, {seconds:.1f} seconds"
        if result is None:
            return LiveCheck(executor_name, False, f"{stopped or 'executor did not run'} ({stats}): {said}")
        if result.timed_out:
            return LiveCheck(executor_name, False, f"timed out after {stats}; executor said: {said}")
        if result.exit_code != 0:
            return LiveCheck(executor_name, False,
                             f"exit code {result.exit_code} ({stats}); executor said: {said}")
        if result.num_turns == 0 or stopped:
            return LiveCheck(executor_name, False,
                             f"{stopped or 'stopped before any work'} ({stats}); executor said: {said}")
        for command in attempt.parsed.done_when_commands:
            check = _check(command, clone, log, 60)
            if check.exit_code != 0 or check.timed_out:
                return LiveCheck(executor_name, False,
                                 f"Done when check failed: {command} ({stats}); executor said: {said}")
        return LiveCheck(executor_name, True, f"smoke task done in {stats}; executor said: {said}")

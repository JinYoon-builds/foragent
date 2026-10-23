"""Execute, independently check and review one approved snapshot."""

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import islice
import ast
import builtins
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
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
from foragent.processes import run_logged, track_process_group
from foragent.reasons import Reason
from foragent.skills import SkillManager, copy_lane_skills
from foragent.state import Approval, StateStore
from foragent.tracker import RecordingTracker, Tracker, TrackerError

from foragent import runner_core
from foragent.runner_core import (  # noqa: F401 - re-exported facade names
    AcceptanceBaseline,
    AttemptFailure,
    CIFailure,
    CheckResult,
    ConflictBudgetExhausted,
    ExecutorStopped,
    PatchReplayError,
    RunContext,
    _append,
    _available_executor,
    _check,
    _ci_failure,
    _credited_wait,
    _executor_wait,
    _hold_changed_head,
    _merge_lock,
    _phase,
    _progress,
    _remaining,
    _run_executor,
    _test_slot,
    _timeout,
    poll_executor,
    wait_for_rate_limit,
)
from foragent import runner_prompts
from foragent.runner_prompts import (  # noqa: F401 - re-exported facade names
    REVIEW_PROMPT,
    ReviewResult,
    _claude_review_scope,
    _normalized_findings,
    _pr_body,
    _read_rebuttals,
    build_prompt,
    build_resume_prompt,
    build_review_prompt,
    goal_condition,
    parse_code_review,
    parse_review,
    review_scope_rules,
)
from foragent import runner_checks
from foragent.runner_checks import (  # noqa: F401 - re-exported facade names
    _baseline_environment,
    _capture_acceptance_baseline,
    _claude_run_dir,
    _passing_acceptance_ids,
    _pytest_command,
    _pytest_invocation,
    _related_unit_tests,
    _write_claude_settings,
    check_done,
    junit_node_id,
    run_tests,
)
from foragent import runner_review
from foragent.runner_review import (  # noqa: F401 - re-exported facade names
    _acceptance_matches,
    _batch_pr,
    _check_publication,
    _inject_lane_skills,
    _lane_notes,
    _lane_skill_names,
    _lane_skills,
    _next_review_round,
    _preserve_failed_implementation,
    _review,
    _review_only,
)
from foragent import runner_attempt
from foragent import runner_publish
from foragent.runner_publish import (  # noqa: F401 - re-exported facade names
    _implement_and_publish,
)

_SPLIT_MODULES = (runner_core, runner_prompts, runner_checks, runner_review, runner_attempt, runner_publish)


def _builtin_reads(module: types.ModuleType) -> set[str]:
    """Builtin names that code in ``module`` looks up as globals."""
    pending = [value.__code__ for value in vars(module).values() if getattr(value, "__module__", None) == module.__name__
               and hasattr(value, "__code__")]
    pending += [
        member.__code__
        for value in vars(module).values()
        if isinstance(value, type) and value.__module__ == module.__name__
        for member in vars(value).values()
        if hasattr(member, "__code__")
    ]
    names: set[str] = set()
    while pending:
        code = pending.pop()
        names.update(code.co_names)
        pending.extend(const for const in code.co_consts if isinstance(const, types.CodeType))
    return names & set(dir(builtins))


# Fixed at import, so deleting and later restoring a name reaches the same modules.
_PATCH_TARGETS = {module: set(vars(module)) | _builtin_reads(module) for module in _SPLIT_MODULES}


class _RunnerFacade(types.ModuleType):
    """Keep ``foragent.runner`` as the one patch point for the split runner modules.

    Each sibling module binds the names it uses at import time. Setting or
    deleting a name here updates every sibling binding of that name too, so a
    patched ``foragent.runner`` attribute still reaches all of ``run_issue``.
    """

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module, names in _PATCH_TARGETS.items():
            if name in names:
                setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module, names in _PATCH_TARGETS.items():
            if name in names and name in vars(module):
                delattr(module, name)


sys.modules[__name__].__class__ = _RunnerFacade


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
        run = ctx.store.update_run(n, pid=os.getpid())
        if until := ctx.store.github_wait_until(ctx.now()):
            outcome = orch_github.wait_run(ctx, n, until)
            return outcome
        board.sync_status(ctx, n, "Running")
        lane = ctx.config.lanes.get(run.lane, {}) if run.lane != "main" else {}
        ctx = replace(ctx, config=replace(
            ctx.config, executor=lane.get("executor") or ctx.config.executor,
            executor_model=lane.get("model") if lane.get("model") is not None else ctx.config.executor_model,
        ))
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

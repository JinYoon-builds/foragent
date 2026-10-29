"""Executor sessions, availability recovery and native Claude settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shlex
import sys
from typing import TYPE_CHECKING

from foragent import board, cuts, usage_events, usage_recheck
from foragent.actions import flag
from foragent.config import resolved_reviewer
from foragent.executors import ExecResult, Executor, classify_result, stream_session_id
from foragent.labels import READY, RUNNING
from foragent.processes import track_process_group
from foragent.reasons import Reason
from foragent.run_attempt import ExecutorStopped
from foragent.run_waits import _executor_wait, _progress

if TYPE_CHECKING:
    from foragent.runner import RunContext


def _claude_run_dir(snapshot: Path) -> Path:
    return snapshot.parent.parent / "runs" / f"issue-{snapshot.stem}"


def _write_claude_settings(n: int, ctx: RunContext, worktree: Path) -> Path:
    snapshot = (ctx.store.root / "approvals" / f"{n}.json").resolve()
    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop-block-count").write_text("0", encoding="utf-8")
    acceptance = Path(os.path.relpath(worktree / ctx.config.acceptance_dir, worktree)).as_posix().rstrip("/") + "/"
    command = shlex.join([
        sys.executable, "-m", "foragent.cli", "gate", "check-done", "--snapshot", str(snapshot),
        "--worktree", str(worktree.resolve()), "--acceptance-dir", ctx.config.acceptance_dir,
    ])
    settings = {
        "permissions": {"deny": [
            f"Edit({acceptance}**)", f"Write({acceptance}**)",
            "Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
            "Bash(git merge:*)",
        ]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command,
                                      "timeout": max(900, ctx.config.max_runtime_minutes * 60)}]}]},
    }
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _run_executor(n: int, ctx: RunContext, executor: Executor, *, spender: str | None = None,
                  **kwargs) -> ExecResult:
    log = kwargs["log_path"]
    size = log.stat().st_size if log.exists() else 0
    offset = size

    def poll():
        nonlocal size
        current = log.stat().st_size if log.exists() else 0
        if current > size:
            timestamp = ctx.now().astimezone(UTC).isoformat()
            ctx.store.update_run(n, last_progress_at=timestamp, last_activity_at=timestamp)
        size = current

    def execute():
        with track_process_group(
            lambda pgid: ctx.store.update_run(n, executor_pgid=pgid),
            tree_callback=lambda identities: ctx.store.update_run(n, process_identities=identities),
        ):
            return executor.run(**kwargs)

    usage: dict = {}
    try:
        result = ctx.poll_hook(execute, poll)
        result.usage = usage  # filled in ``finally`` once the log is complete
        if spender is not None:  # Money this session spent, from the executor's own result (#786).
            from foragent.quota_spend import record_cost

            record_cost(ctx.store, spender, result.cost_usd)
        if log.exists():
            result.log_tail = cuts.read_tail(log, offset, 4000, "executor", cuts.issue_dir(ctx.store.root, n))
        return result
    finally:
        poll()
        # Executors report plan usage in their own output; keep the latest numbers.
        usage.update(usage_events.record_from_log(ctx.store, log, offset, ctx.now()))
        _progress(n, ctx)


def _session_hold(ctx: RunContext, name: str, lane: str | None) -> str | None:
    """Why a session of ``name`` may not launch now; the same quota rules ``decide`` applies."""
    from foragent.scheduler_quota import blocked, entry_for

    from foragent.scheduler_quota import _usage

    now = ctx.now()
    entry = entry_for(ctx.config, name)
    why = blocked(entry, ctx.store, now, _usage(ctx.store), "keep", keep=False)[0]
    if why is not None and why.key != "switch_reason_limit":  # A plain pause keeps today's words below.
        return why.reason + "."
    if entry.get("quota") == "subscription" and entry.get("spend_cap") is not None:
        return None  # Credits under the cap run past the executor pause; a lane pause mirrors it.
    if (until := usage_recheck.hold_lane(ctx.store, name, lane, now)) is not None:
        return f"{name} is paused for a usage limit until {until.isoformat()}."
    if why is not None:  # A used-up window holds a launch as an executor pause does (#186).
        return f"{name} is paused for a usage limit until {why.values['until']}."
    return None


def lane_default(ctx: RunContext, run) -> bool:
    """Whether ``run`` runs on its lane's configured executor; only then does a limit pause the lane."""
    if getattr(run, "executor", None) is None:  # A record from before #186.
        return run.implementer is None
    return run.executor == (ctx.lane_executor or ctx.config.executor)


def _limit_wait(n: int, ctx: RunContext, detail: str) -> None:
    """Hand the issue back to the queue; ``usage_recheck`` already wrote the executor and lane pauses."""
    ctx.store.update_run(n, detail=detail)
    ctx.tracker.add_labels(n, [READY])
    ctx.tracker.remove_labels(n, [RUNNING])
    board.sync_status(ctx, n, "Ready")
    raise ExecutorStopped("limit-wait")


def _available_executor(
    n: int, ctx: RunContext, executor: Executor, *, reviewer: bool = False,
    session_body: str | None = None, resume_prompt: str | None = None, flag_exhaustion: bool = True,
    on_result: Callable[[ExecResult], None] | None = None,
    **kwargs,
) -> ExecResult:
    """Run until the executor is available; ``flag_exhaustion=False`` leaves the block to the caller.

    ``on_result`` sees every result the executor returns, including the ones a usage limit,
    a transient retry or transient exhaustion discards, before this function decides on it.
    """
    from foragent import runner
    from foragent.run_checks import _append
    from foragent.scheduler_quota import credits

    errors = 0
    full_prompt = kwargs["prompt"]

    def execute() -> ExecResult:
        # Every launch, including a fresh start after a failed resume and each retry, is admitted
        # here: another run may have paused this executor, or this run reached its spend_cap (#786).
        # A keep-mode switched run (#107) never pauses its lane: the lane's own pair may be free.
        current = ctx.store.get_run(n)
        if (hold := _session_hold(ctx, name, current.lane if lane_default(ctx, current) else None)) is not None:
            _limit_wait(n, ctx, hold)
        log = kwargs["log_path"]
        offset = log.stat().st_size if log.exists() else 0
        try:
            spender = name if credits(ctx.store, ctx.config, name, ctx.now()) else None
            result = _run_executor(n, ctx, executor, spender=spender, **kwargs)
            if on_result is not None:
                on_result(result)
            return result
        except BaseException:
            # run_logged flushes stdout into the log during interruption cleanup.
            # Recover this call's lifecycle before run_issue finishes its history.
            if session_body is not None and not reviewer and log.exists():
                identity = stream_session_id(log, ctx.config.executor, offset=offset)
                if identity is not None:
                    current = ctx.store.get_run(n)
                    ctx.store.update_run(
                        n, session_id=identity, session_executor=ctx.config.executor,
                        session_ids=[*current.session_ids, identity], session_body=session_body,
                    )
            raise

    name = resolved_reviewer(ctx.config) if reviewer else ctx.config.executor
    # Feedback saved for this turn stays until the implementer has actually received it.
    saved = ctx.store.get_run(n)
    pending, pending_whole = (saved.resume_reason, saved.resume_whole) if session_body is not None else ("", False)
    while True:
        run = ctx.store.get_run(n)
        if not reviewer and run.fallback_model is not None:
            kwargs["model"] = run.fallback_model
        kwargs["timeout_s"] = runner._remaining(n, ctx)
        session_id = (run.session_id if session_body is not None and resume_prompt is not None and not reviewer
                      and run.session_executor == ctx.config.executor else None)
        kwargs["prompt"] = resume_prompt if session_id is not None else full_prompt
        if session_id is not None:
            kwargs["resume_session_id"] = session_id
        else:
            kwargs.pop("resume_session_id", None)
        try:
            result = execute()
        except OSError as exc:
            if session_id is None:
                raise
            _append(kwargs["log_path"], f"Session resume could not launch; starting fresh: {exc}")
            result = None
        if session_id is not None and (result is None or (
            result.session_id is None and result.exit_code not in {None, 0}
            and not result.timed_out and classify_result(result) == "normal"
        )):
            ctx.store.update_run(n, resume_failures=run.resume_failures + 1)
            if result is not None:
                _append(kwargs["log_path"], f"Session resume failed; starting fresh:\n{result.last_message}\n{result.log_tail}")
            session_id = None
            kwargs.pop("resume_session_id")
            kwargs["prompt"] = full_prompt
            kwargs["timeout_s"] = runner._remaining(n, ctx)
            result = execute()
        kind = classify_result(result)
        if not reviewer and result.num_turns == 0 and not result.timed_out and kind == "normal":
            detail = result.last_message or result.log_tail or "Executor returned without an implementation turn."
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Executor stopped before implementation started.", detail)
            raise ExecutorStopped("executor-error", detail)
        if session_body is not None and not reviewer:
            identity = result.session_id or session_id
            reason = (f"Executor exit_code={result.exit_code}, timed_out={result.timed_out}\n"
                      f"{result.last_message}\n{result.log_tail}" if result.exit_code != 0 or result.timed_out else "")
            run = ctx.store.update_run(
                n, session_id=identity, session_executor=ctx.config.executor,
                session_ids=[*run.session_ids, *([identity] if identity is not None else [])],
                session_body=(session_body if result.session_id is not None or (
                    result.exit_code == 0 and not result.timed_out
                ) else run.session_body),
                resume_reason=(f"{pending}\n\n{reason}" if pending else reason) if reason else "",
                resume_whole=bool(reason and pending and pending_whole),
            )
            if reason:
                resume_prompt = (resume_prompt or "") + "\n\n" + reason
        if kind == "normal":
            return result
        if kind == "transient" and runner._remaining(n, ctx) <= 0:
            return replace(result, timed_out=True)
        if kind == "limit":
            now = ctx.now()
            windows = (result.usage or {}).get(name)
            until = usage_events.limit_reset(windows, now, now + timedelta(minutes=ctx.config.limit_wait_minutes))
            # One write pauses the executor and lane and notes the levels; the gate may end it early.
            usage_recheck.note_limit(ctx.store, name, until, windows, now,
                                     lane=run.lane if lane_default(ctx, run) else None)
            _limit_wait(n, ctx, result.last_message or result.log_tail or "Executor usage limit reached.")
        if kind == "transient" and not reviewer:
            run = ctx.store.update_run(n, transient_errors=run.transient_errors + 1)
            if run.transient_errors >= 2 and ctx.config.executor_fallback_model and run.fallback_model is None:
                ctx.store.update_run(n, fallback_model=ctx.config.executor_fallback_model)
                _append(kwargs["log_path"], f"Executor fallback model: {ctx.config.executor_fallback_model}")
        budget = ctx.config.transient_max_wait_minutes * 60 - ctx.store.get_run(n).executor_waited_seconds
        if budget <= 0:
            detail = result.last_message or result.log_tail
            if flag_exhaustion:
                flag(ctx, n, Reason.RETRIES_EXCEEDED, "Executor unavailable (transient errors)", detail)
            raise ExecutorStopped("retries-exceeded", detail)
        _executor_wait(n, ctx, min(120 * 2 ** min(errors, 3), budget))
        errors += 1

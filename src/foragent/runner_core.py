"""Shared run context, failures and executor plumbing for the runner."""

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading
import time
from foragent import board, capacity, orch_github
from foragent.actions import flag
from foragent.config import Config
from foragent.executors import ExecResult, Executor, classify_result, executor_for, stream_session_id
from foragent.git import GitError, GitRepo
from foragent.labels import READY, RUNNING
from foragent.processes import run_logged, track_process_group
from foragent.reasons import Reason
from foragent.state import StateStore
from foragent.tracker import RecordingTracker, Tracker, TrackerError


def poll_executor[T](execute: Callable[[], T], poll: Callable[[], None]) -> T:
    """Keep execution on the main thread so subprocess termination still works."""
    stopped = threading.Event()

    def watch():
        while not stopped.wait(30):
            poll()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return execute()
    finally:
        stopped.set()
        watcher.join()


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


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    timed_out: bool
    output: str


@dataclass(frozen=True)
class AcceptanceBaseline:
    command: str
    node_ids: tuple[str, ...]


class PatchReplayError(GitError):
    """A child patch could not be replayed onto its selected destination."""


class AttemptFailure(Exception):
    """Checks failed and need another implementation attempt."""

    def __init__(self, message: str, target_branch: str = ""):
        super().__init__(message)
        self.target_branch = target_branch


class ConflictBudgetExhausted(Exception):
    """Conflict resolution needs one fresh implementation on the latest target."""


class CIFailure(AttemptFailure):
    def __init__(self, logs: str, target_branch: str):
        super().__init__(f"CI failed:\n{logs}")
        self.target_branch = target_branch


class ExecutorStopped(Exception):
    def __init__(self, outcome: str, detail: str = ""):
        super().__init__(detail)
        self.outcome = outcome


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def _check(
    command: str, worktree: Path, log: Path, timeout_s: int,
    *, progress: Callable[[], None] | None = None,
    slot: AbstractContextManager | None = None,
) -> CheckResult:
    _append(log, f"\nDone when command:\n{command}")
    offset = log.stat().st_size

    def execute():
        # The slot covers only the command; timeout_s counts execution, not the queue.
        if slot is None:
            return run_logged(["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log)
        with slot:
            # A nested `fa test` in the command shares this slot instead of waiting for another.
            return run_logged(["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
                              env=capacity.HELD_ENV)

    # A quiet check is still running; keep the scheduler informed while it waits.
    code, timed_out, _ = poll_executor(execute, progress) if progress is not None else execute()
    with log.open("rb") as stream:
        stream.seek(max(offset, log.stat().st_size - 16000))
        tail = stream.read().decode("utf-8", errors="replace")[-4000:]
    _append(log, f"\nCheck exit_code={code}, timed_out={timed_out}\nOutput tail:\n{tail}")
    return CheckResult(command, code, timed_out, tail)


def _remaining(n: int, ctx: RunContext) -> int:
    run = ctx.store.get_run(n)
    if run is None:
        raise KeyError(n)
    elapsed = (ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds() - run.waited_seconds
    return max(0, int(ctx.config.max_runtime_minutes * 60 - elapsed))


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


def _hold_changed_head(n: int, ctx: RunContext, link: str) -> str:
    flag(ctx, n, Reason.RISK_HIGH, "PR head changed after verification", link)
    return "held"


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    failure = CIFailure(ctx.tracker.pr_failed_logs(pr), target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise ExecutorStopped(_hold_changed_head(n, ctx, link))
    run = ctx.store.get_run(n)
    _append(ctx.store.log_path(n), f"Attempt {run.attempts} failed:\n{failure}")
    if run.attempts >= ctx.config.max_retries + 1:
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "CI failed after all attempts.", f"{failure}\n\n{link}")
        raise ExecutorStopped("ci-failed", str(failure))
    ctx.store.update_run(n, attempts=run.attempts + 1)
    raise failure


@contextmanager
def _test_slot(n: int, ctx: RunContext):
    """Hold a machine-wide test slot; credit the queue time as waited, not run time."""
    run = ctx.store.get_run(n)
    started = ctx.now()
    waited = False

    def credit() -> datetime:
        now = ctx.now()
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0.0, (now - started).total_seconds()),
            last_progress_at=now.astimezone(UTC).isoformat(),
        )
        return now

    def waiting() -> None:
        nonlocal waited
        waited = True
        now = credit()
        # Tick reads a future waiting_until as alive; refresh it while queued.
        ctx.store.update_run(n, waiting_until=(now + timedelta(seconds=60)).astimezone(UTC).isoformat())

    with ExitStack() as stack:
        try:
            stack.enter_context(capacity.hold_test_slot(ctx.config.test_slots, waiting=waiting))
        finally:
            if waited:
                credit()
                ctx.store.update_run(n, waiting_until=None)
        yield


def _progress(n: int, ctx: RunContext) -> None:
    ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())


def _phase(n: int, ctx: RunContext, phase: str) -> None:
    if ctx.store.get_run(n).phase != phase:
        ctx.store.update_run(n, phase=phase, phase_started_at=ctx.now().astimezone(UTC).isoformat())


def _run_executor(n: int, ctx: RunContext, executor: Executor, **kwargs) -> ExecResult:
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
        with track_process_group(lambda pgid: ctx.store.update_run(n, executor_pgid=pgid)):
            return executor.run(**kwargs)

    try:
        result = ctx.poll_hook(execute, poll)
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(offset, log.stat().st_size - 16000))
                result.log_tail = stream.read().decode("utf-8", errors="replace")[-4000:]
        return result
    finally:
        poll()
        _progress(n, ctx)


def wait_for_rate_limit(n: int, ctx: RunContext, error: TrackerError) -> bool:
    """Persist a resumable wait through the shared GitHub availability mechanism."""
    if not error.is_rate_limit:
        return False
    orch_github.wait_run(ctx, n, orch_github.pause(ctx, error))
    return True


def _executor_wait(n: int, ctx: RunContext, seconds: float) -> None:
    """Persist the wait before sleeping so tick cannot mistake it for a stall."""
    run = ctx.store.get_run(n)
    started = ctx.now()
    started_mono = ctx.monotonic()
    ctx.store.update_run(
        n, waited_seconds=run.waited_seconds + seconds,
        executor_waited_seconds=run.executor_waited_seconds + seconds,
        waiting_until=(started + timedelta(seconds=seconds)).astimezone(UTC).isoformat(),
        last_progress_at=started.astimezone(UTC).isoformat(),
    )
    try:
        ctx.sleep(seconds)
    finally:
        elapsed = max(0, ctx.monotonic() - started_mono)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + elapsed,
            executor_waited_seconds=run.executor_waited_seconds + elapsed,
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )


def _credited_wait(n: int, ctx: RunContext, acquire: Callable[[], object]) -> None:
    """Block in acquire; credit the time as waited and keep tick informed meanwhile."""
    run = ctx.store.get_run(n)
    started = ctx.monotonic()

    def progress():
        now = ctx.now().astimezone(UTC)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=(now + timedelta(seconds=60)).isoformat(), last_progress_at=now.isoformat(),
        )

    progress()
    try:
        poll_executor(acquire, progress)
    finally:
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )


@contextmanager
def _merge_lock(n: int, ctx: RunContext):
    # Keep tick informed during an unbounded blocking flock acquisition.
    with ExitStack() as stack:
        _credited_wait(n, ctx, lambda: stack.enter_context(ctx.store.merge_lock()))
        yield


def _available_executor(
    n: int, ctx: RunContext, executor: Executor, *, reviewer: bool = False,
    session_body: str | None = None, resume_prompt: str | None = None, **kwargs,
) -> ExecResult:
    errors = 0
    full_prompt = kwargs["prompt"]

    def execute() -> ExecResult:
        log = kwargs["log_path"]
        offset = log.stat().st_size if log.exists() else 0
        try:
            return _run_executor(n, ctx, executor, **kwargs)
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

    while True:
        run = ctx.store.get_run(n)
        if not reviewer and run.fallback_model is not None:
            kwargs["model"] = run.fallback_model
        kwargs["timeout_s"] = _remaining(n, ctx)
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
            if result is not None:
                _append(kwargs["log_path"], f"Session resume failed; starting fresh:\n{result.last_message}\n{result.log_tail}")
            session_id = None
            kwargs.pop("resume_session_id")
            kwargs["prompt"] = full_prompt
            kwargs["timeout_s"] = _remaining(n, ctx)
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
                ) else run.session_body), resume_reason=reason,
            )
            if reason:
                resume_prompt = (resume_prompt or "") + "\n\n" + reason
        if kind == "normal":
            return result
        if kind == "transient" and _remaining(n, ctx) <= 0:
            return replace(result, timed_out=True)
        if kind == "limit" and not reviewer:
            ctx.store.update_run(n, detail=result.last_message or result.log_tail or "Executor usage limit reached.")
            ctx.store.pause_lane(run.lane, ctx.now() + timedelta(minutes=ctx.config.limit_wait_minutes))
            ctx.tracker.add_labels(n, [READY])
            ctx.tracker.remove_labels(n, [RUNNING])
            board.sync_status(ctx, n, "Ready")
            raise ExecutorStopped("limit-wait")
        if kind == "transient" and not reviewer:
            run = ctx.store.update_run(n, transient_errors=run.transient_errors + 1)
            if run.transient_errors >= 2 and ctx.config.executor_fallback_model and run.fallback_model is None:
                ctx.store.update_run(n, fallback_model=ctx.config.executor_fallback_model)
                _append(kwargs["log_path"], f"Executor fallback model: {ctx.config.executor_fallback_model}")
        budget = ctx.config.transient_max_wait_minutes * 60 - ctx.store.get_run(n).executor_waited_seconds
        if budget <= 0:
            if reviewer and kind == "limit":
                return replace(result, exit_code=result.exit_code or 1)
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Executor unavailable (transient errors)",
                 result.last_message or result.log_tail)
            raise ExecutorStopped("retries-exceeded")
        _executor_wait(n, ctx, min(120 * 2 ** min(errors, 3), budget))
        errors += 1

"""CI waits and CI failure handling for publication."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from pathlib import Path


def _hold_changed_head(n: int, ctx: RunContext, link: str) -> str:
    run_publish.flag(ctx, n, run_publish.Reason.RISK_HIGH, "PR head changed after verification", link)
    return "held"


def _head_after_push(ctx: RunContext, pr: int, head: str, replaced: str | None) -> bool:
    """Wait only for the exact head replaced by our successful leased push."""
    delays = iter((2, 4, 8, 16, 30))
    while True:
        reads = getattr(ctx, "reads", None)
        if reads is not None:
            reads.clear()  # Both the owner snapshot and its underlying gh GET cache.
        actual = ctx.tracker.pr_head_sha(pr)
        if actual == head:
            return True
        if not replaced or actual != replaced:
            return False
        delay = next(delays, None)
        if delay is None:
            return False
        ctx.sleep(delay)


def _hold_if_not_started(n: int, ctx: RunContext, pr: int, logs: str) -> None:
    """Jobs that never ran are infrastructure: hold with the evidence, keep the PR open."""
    if not getattr(logs, "not_started", False):
        return
    run_publish._record_result(n, ctx, "not_started")
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    run_publish._append(ctx.store.log_path(n), f"CI did not start:\n{logs}")
    run_publish.flag(ctx, n, run_publish.Reason.CI_NOT_STARTED,
                     "CI jobs did not start; there is no code failure to repair.", f"{logs}\n\n{link}")
    raise run_publish.ExecutorStopped("held", str(logs))


def _record_failure(n: int, ctx: RunContext, pr: int, head: str) -> None:
    """Keep a CI code failure as a local fact for the scorecard; recording never changes the run.

    Every exit that handles a code failure of this run's head calls this once, after the
    not-started hold (infrastructure, not code) and before its own retry or block decision."""
    run_publish._record_result(n, ctx, "failed")
    try:
        started = run_publish._failing_execution(n, ctx)
        ctx.store.record_flow("ci-failure", issue=n, run_started_at=started, pr=pr, head=head)
    except Exception:  # noqa: BLE001 - a missing fact is unknown later, never a different outcome now.
        return


def _failing_execution(n: int, ctx: RunContext) -> str | None:
    """The execution a CI failure belongs to, from a record, never from whichever run is live now.

    Inside the run's own CI wait it is that run. Outside it, only the merge owner meets a CI
    failure, and it works on the issue's admitted execution (its own admission record). A newer
    direct rerun is neither, so it is never credited with the owner's failure. Unknown: None."""
    identity = _WAITING_RUN.get()
    if identity is not None and identity[0] == n:
        return identity[1]
    from foragent import merge_owner

    admitted = {started for issue, started in merge_owner.admissions(ctx.store) if issue == n}
    return admitted.pop() if len(admitted) == 1 else None


# The run whose own CI wait is executing, as (issue, started_at): the only writer of its CI result.
_WAITING_RUN: ContextVar[tuple[int, str] | None] = ContextVar("waiting_run", default=None)


@contextmanager
def _run_ci_wait(n: int, ctx: RunContext):
    """Mark the code that is the run's own CI wait, so what it concludes is that run's CI result.

    Taken once at entry from the run's live record: only the run's own flows enter here (the CI
    wait and the comparison right after it). The merge owner's reads, holds and comparisons never
    enter, so they never write a run's result, whichever run is live at that moment."""
    try:
        run = ctx.store.get_run(n)
        identity = (n, run.started_at) if run is not None and run.status == "running" else None
    except Exception:  # noqa: BLE001 - without an identity nothing is recorded; the wait itself goes on.
        identity = None
    token = _WAITING_RUN.set(identity)
    try:
        yield
    finally:
        _WAITING_RUN.reset(token)


def _record_result(n: int, ctx: RunContext, result: str) -> None:
    """Save what this run's CI wait concluded as the run's own ``ci_result`` (state_runs.CI_RESULTS).

    Only inside the run's own CI wait (``_run_ci_wait``) and only into that execution's record:
    pass, no checks, timeout, jobs that never started, or a code failure (``_record_failure``).
    The latest conclusion of the run's waits stands; recording never changes the run."""
    identity = _WAITING_RUN.get()
    if identity is None or identity[0] != n:
        return
    try:
        ctx.store.record_ci_result(n, identity[1], result)
    except Exception:  # noqa: BLE001 - a missing fact is unknown later, never a different outcome now.
        return


def require_comparison(attempt, *args, **kwargs):
    """The run's comparison-mode check after its CI wait: part of that wait, so a regression it
    finds is the run's CI result. The merge owner calls ``ci_mode.require_comparison`` directly."""
    from foragent import ci_mode

    n, ctx = getattr(attempt, "n", None), getattr(attempt, "ctx", None)
    if not isinstance(n, int) or ctx is None:
        return ci_mode.require_comparison(attempt, *args, **kwargs)
    with _run_ci_wait(n, ctx):
        return ci_mode.require_comparison(attempt, *args, **kwargs)


STATUS_RESULTS = {"pass": "passed", "none": "no_checks", "fail": "failed"}


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    logs = ctx.tracker.pr_failed_logs(pr, full_text_dir=run_publish.cuts.issue_dir(ctx.store.root, n))
    if not logs.strip():
        logs = "(CI evidence unavailable: GitHub reported a failure but gave no matching run or job evidence)"
    failure = run_publish.CIFailure(logs, target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise run_publish.ExecutorStopped(run_publish._hold_changed_head(n, ctx, link))
    run_publish._hold_if_not_started(n, ctx, pr, logs)
    run = ctx.store.get_run(n)
    run_publish._record_failure(n, ctx, pr, head)
    run_publish._append(ctx.store.log_path(n), f"Attempt {run.attempts} failed:\n{failure}")
    if run.attempts >= ctx.config.max_retries + 1:
        run_publish.flag(ctx, n, run_publish.Reason.RETRIES_EXCEEDED, "CI failed after all attempts.", f"{failure}\n\n{link}")
        raise run_publish.ExecutorStopped("ci-failed", str(failure))
    run_publish._phase(n, ctx, "implementing")
    ctx.store.update_run(n, attempts=run.attempts + 1)
    raise failure


def _ci_status(ctx: RunContext, pr: int, worktree: Path, ready_at: float) -> str:
    status = ctx.tracker.pr_checks(pr)
    has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                        for path in (worktree / ".github/workflows").glob(pattern))
    if status == "none" and has_workflows and ctx.now().timestamp() - ready_at < 180:
        return "pending"
    return status


QUEUE_CEILING = 3 * 60 * 60


@dataclass
class _CIWait:
    started: float
    polls: int = 0
    status: str = "pending"
    infrastructure: str = ""
    running_since: float | None = None
    queue_aware: bool = False

    @property
    def deadline(self) -> float:
        if not self.queue_aware:
            return self.started + 1200
        ceiling = self.started + QUEUE_CEILING
        return min(self.running_since + 1200, ceiling) if self.running_since is not None else ceiling


def _wait_for_ci(
    n: int, ctx: RunContext, git: GitRepo, pr: int, head: str, ready_at: float,
    wait: _CIWait, failed: Callable[[], None], status: str | None = None,
    *, comparison_mode: bool = False, conflicting: Callable[[], bool] | None = None,
) -> str | None:
    """Consume even a status observed under the lock, but only wait unlocked.

    ``conflicting()`` answering True before a wait raises ConflictingPR: GitHub
    starts no CI for a head that conflicts with its base. It is the run's own CI wait, so what it
    concludes (and what its ``failed`` callback concludes) is recorded as the run's CI result."""
    with _run_ci_wait(n, ctx):
        return _wait(n, ctx, git, pr, head, ready_at, wait, failed, status,
                     comparison_mode=comparison_mode, conflicting=conflicting)


def _wait(n, ctx, git, pr, head, ready_at, wait, failed, status, *, comparison_mode, conflicting):
    from foragent import runner

    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    wait.infrastructure = ""
    while True:
        if runner._remaining(n, ctx) <= 0:
            run_publish._record_result(n, ctx, "timed_out")
            return run_publish._timeout(n, ctx)
        if status is None:
            status = run_publish._ci_status(ctx, pr, git.root, ready_at)
        started_reader = getattr(ctx.tracker, "pr_checks_started", None)
        wait.queue_aware = callable(started_reader)
        if wait.queue_aware:
            started_at = started_reader(pr)
            if started_at is not None:
                since = max(wait.started, started_at.timestamp())
                wait.running_since = min(wait.running_since, since) if wait.running_since is not None else since
        if wait.queue_aware and wait.running_since is None and status in {"pass", "none"}:
            # No queued checks remain. Missing start metadata keeps the ordinary comparison window.
            wait.running_since = wait.started
        wait.status = status
        run_publish._progress(n, ctx)
        if ctx.tracker.pr_head_sha(pr) != head:
            return run_publish._hold_changed_head(n, ctx, link)
        if status in {"pass", "none"}:
            run_publish._record_result(n, ctx, STATUS_RESULTS[status])
            return None
        if status == "fail":
            # GitHub's failing checks are the result read; recorded before any log read can be cut
            # short (a rate limit). Jobs that never started replace it below or in _ci_failure.
            run_publish._record_result(n, ctx, "failed")
            if comparison_mode:
                evidence = ctx.tracker.pr_failed_logs(pr)
                if getattr(evidence, "not_started", False):
                    run_publish._record_result(n, ctx, "not_started")
                    wait.infrastructure = str(evidence)
                    return None
            failed()
        remaining = wait.deadline - ctx.now().timestamp()
        if remaining <= 0 or wait.polls >= 40:
            if comparison_mode and ctx.config.checks_fallback == "local":
                run_publish._record_result(n, ctx, "timed_out")
                wait.infrastructure = "CI did not finish before the poll deadline."
                return None
            detail = ("CI never left the queue before the 3-hour ceiling."
                      if wait.queue_aware and wait.running_since is None
                      else "CI ran past the wait limit.")
            run_publish._record_result(n, ctx, "timed_out")  # before flag(), which can be rate limited
            run_publish.flag(ctx, n, run_publish.Reason.TIMEOUT, "CI did not finish", f"{detail}\n\n{link}")
            return "timeout"
        if conflicting is not None and conflicting():
            from foragent.ci_mode import ConflictingPR

            raise ConflictingPR(head)
        ctx.sleep(min(30, remaining, runner._remaining(n, ctx)))
        if not wait.queue_aware or wait.running_since is not None:
            wait.polls += 1
        status = None


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

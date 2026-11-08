"""CI waits and CI failure handling for publication."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

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
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    run_publish._append(ctx.store.log_path(n), f"CI did not start:\n{logs}")
    run_publish.flag(ctx, n, run_publish.Reason.CI_NOT_STARTED,
                     "CI jobs did not start; there is no code failure to repair.", f"{logs}\n\n{link}")
    raise run_publish.ExecutorStopped("held", str(logs))


def _record_failure(n: int, ctx: RunContext, pr: int, head: str) -> None:
    """Keep a CI code failure as a local fact for the scorecard; recording never changes the run.

    Every exit that handles a code failure of this run's head calls this once, after the
    not-started hold (infrastructure, not code) and before its own retry or block decision."""
    try:
        run = ctx.store.get_run(n)
        ctx.store.record_flow("ci-failure", issue=n, run_started_at=run.started_at, pr=pr, head=head)
    except Exception:  # noqa: BLE001 - a missing fact is unknown later, never a different outcome now.
        return


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


def _has_workflows(worktree: Path) -> bool:
    return any(path.is_file() for pattern in ("*.yml", "*.yaml")
               for path in (worktree / ".github/workflows").glob(pattern))


def _ci_status(ctx: RunContext, pr: int, worktree: Path, ready_at: float) -> str:
    status = ctx.tracker.pr_checks(pr)
    if status == "none" and _has_workflows(worktree) and ctx.now().timestamp() - ready_at < 180:
        return "pending"
    return status


CI_WAIT_SAFETY_NET_SECONDS = 6 * 60 * 60


@dataclass
class CIWait:
    started: float
    polls: int = 0
    status: str = "pending"
    infrastructure: str = ""

    @property
    def deadline(self) -> float:
        return self.started + CI_WAIT_SAFETY_NET_SECONDS


@contextmanager
def ci_wait_credit(n: int, ctx: RunContext):
    """Register a CI wait before polling; close its union credit on every exit."""
    started = ctx.now()
    key = f"ci-wait-{uuid4().hex}"

    def credit(close: bool = False) -> None:
        now = ctx.now()
        ctx.store.add_waited(
            n, key=key, start=started, end=now, close=close,
            waiting_until=None if close else (now + timedelta(seconds=60)).astimezone(UTC).isoformat(),
            last_progress_at=now.astimezone(UTC).isoformat(),
        )

    credit()
    try:
        yield credit
    finally:
        credit(close=True)

_CIWait = CIWait


def _wait_for_ci(
    n: int, ctx: RunContext, git: GitRepo, pr: int, head: str, ready_at: float,
    wait: _CIWait, failed: Callable[[], None], status: str | None = None,
    *, comparison_mode: bool = False, conflicting: Callable[[], bool] | None = None,
) -> str | None:
    """Consume even a status observed under the lock, but only wait unlocked.

    ``conflicting()`` answering True before a wait raises ConflictingPR: GitHub
    starts no CI for a head that conflicts with its base."""
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    wait.infrastructure = ""
    # Local-only repositories have no CI producer. Their status polling is
    # still bounded, but must not replenish the implementation budget.
    credited = comparison_mode or (git is not None and _has_workflows(git.root))
    with ci_wait_credit(n, ctx) if credited else nullcontext() as credit:
        while True:
            if credit is not None:
                credit()
            if status is None:
                status = run_publish._ci_status(ctx, pr, git.root, ready_at)
            wait.status = status
            run_publish._progress(n, ctx)
            if ctx.tracker.pr_head_sha(pr) != head:
                return run_publish._hold_changed_head(n, ctx, link)
            if status in {"pass", "none"}:
                return None
            if status == "fail":
                if comparison_mode:
                    evidence = ctx.tracker.pr_failed_logs(pr)
                    if getattr(evidence, "not_started", False):
                        wait.infrastructure = str(evidence)
                        return None
                failed()
            remaining = wait.deadline - ctx.now().timestamp()
            if remaining <= 0:
                detail = "CI reached the 6-hour safety net."
                if comparison_mode and ctx.config.checks_fallback == "local":
                    wait.infrastructure = detail
                    return None
                run_publish.flag(ctx, n, run_publish.Reason.TIMEOUT, "CI did not finish", f"{detail}\n\n{link}")
                return "timeout"
            if conflicting is not None and conflicting():
                from foragent.ci_mode import ConflictingPR

                raise ConflictingPR(head)
            ctx.sleep(min(30, remaining))
            wait.polls += 1
            status = None


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

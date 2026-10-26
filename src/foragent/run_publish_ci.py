"""CI waits and CI failure handling for publication."""

from __future__ import annotations

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


def _hold_if_not_started(n: int, ctx: RunContext, pr: int, logs: str) -> None:
    """Jobs that never ran are infrastructure: hold with the evidence, keep the PR open."""
    if not getattr(logs, "not_started", False):
        return
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    run_publish._append(ctx.store.log_path(n), f"CI did not start:\n{logs}")
    run_publish.flag(ctx, n, run_publish.Reason.CI_NOT_STARTED,
                     "CI jobs did not start; there is no code failure to repair.", f"{logs}\n\n{link}")
    raise run_publish.ExecutorStopped("held", str(logs))


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    logs = ctx.tracker.pr_failed_logs(pr)
    failure = run_publish.CIFailure(logs, target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise run_publish.ExecutorStopped(run_publish._hold_changed_head(n, ctx, link))
    run_publish._hold_if_not_started(n, ctx, pr, logs)
    run = ctx.store.get_run(n)
    run_publish._append(ctx.store.log_path(n), f"Attempt {run.attempts} failed:\n{failure}")
    if run.attempts >= ctx.config.max_retries + 1:
        run_publish.flag(ctx, n, run_publish.Reason.RETRIES_EXCEEDED, "CI failed after all attempts.", f"{failure}\n\n{link}")
        raise run_publish.ExecutorStopped("ci-failed", str(failure))
    ctx.store.update_run(n, attempts=run.attempts + 1)
    raise failure


def _ci_status(ctx: RunContext, pr: int, worktree: Path, ready_at: float) -> str:
    status = ctx.tracker.pr_checks(pr)
    has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                        for path in (worktree / ".github/workflows").glob(pattern))
    if status == "none" and has_workflows and ctx.now().timestamp() - ready_at < 180:
        return "pending"
    return status


@dataclass
class _CIWait:
    started: float
    polls: int = 0
    status: str = "pending"
    infrastructure: str = ""

    @property
    def deadline(self) -> float:
        return self.started + 1200


def _wait_for_ci(
    n: int, ctx: RunContext, git: GitRepo, pr: int, head: str, ready_at: float,
    wait: _CIWait, failed: Callable[[], None], status: str | None = None,
    *, comparison_mode: bool = False,
) -> str | None:
    """Consume even a status observed under the lock, but only wait unlocked."""
    from foragent import runner

    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    wait.infrastructure = ""
    while True:
        if runner._remaining(n, ctx) <= 0:
            return run_publish._timeout(n, ctx)
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
        if remaining <= 0 or wait.polls >= 40:
            if comparison_mode and ctx.config.checks_fallback == "local":
                wait.infrastructure = "CI did not finish before the poll deadline."
                return None
            run_publish.flag(ctx, n, run_publish.Reason.TIMEOUT, "CI did not finish", link)
            return "timeout"
        ctx.sleep(min(30, remaining, runner._remaining(n, ctx)))
        wait.polls += 1
        status = None


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402

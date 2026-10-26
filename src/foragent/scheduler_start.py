"""Size the machine budget and start ready issues within it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from foragent import machine_budget, scheduler
# The facade re-exports these capacity and baseline bindings from here; calls below go through it.
from foragent.capacity import (  # noqa: F401
    INCREASE_INTERVAL, INITIAL_PARALLEL, LAUNCH_GRACE, append_budget_history, append_memory_hold, decrease_allowed, machine_load,
    machine_lock, machine_runs, next_parallel, read_state, update_test_slots, write_state,
)
from foragent.config import lane_executors
from foragent.orch_tick_outcome import step, timed  # noqa: F401
from foragent.run_baseline import prebuild_acceptance_baseline, start_baseline_thread  # noqa: F401
from foragent.tracker import Issue

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _lane_held(ctx: TickContext, lane: str, now: datetime) -> bool:
    """A lane waits while it, its implementer or its reviewer is paused for a usage limit."""
    if ctx.store.lane_paused(lane, now):
        return True
    return any(ctx.store.executor_paused(name, now) for name in lane_executors(ctx.config, lane))


def _apply_budgets(
    ctx: TickContext, ready: dict[str, list[int]], *, workers: bool = True,
) -> tuple[str, float]:
    """Apply machine.yml and CPU load to the shared budgets once per tick; return its sensor sample.

    Every tick runs this, a GitHub wait included. ``workers=False`` leaves the
    worker budget to the caller, which sizes it with this sample right away.
    """
    if (sample := getattr(ctx, "_budget_sample", None)) is not None:
        return sample  # Already applied in this tick.
    sample, machine = scheduler.machine_load(), machine_budget.values()
    ctx._budget_sample = sample
    scheduler.update_test_slots(
        machine["test_slots"], ctx.now(), pressure=sample[0], load_per_cpu=sample[1],
        swapins=getattr(sample, "swapins", None),
        load_average_per_cpu=getattr(sample, "load_average_per_cpu", None),
        cpu_busy=getattr(sample, "cpu_busy", None),
    )
    if workers:
        scheduler._machine_budget(ctx, ready, sample)
    return sample


def _machine_budget(
    ctx: TickContext, ready: dict[str, list[int]], sample: tuple[str, float] | None = None,
) -> int:
    """Update the machine-wide AIMD value once per tick and return this tick's machine budget."""
    now = ctx.now()
    # Every ready issue in an unpaused lane may start; the file-overlap check runs at start time.
    waiting = sum(len(numbers) for lane, numbers in ready.items() if not _lane_held(ctx, lane, now))
    sample = sample if sample is not None else scheduler.machine_load()
    pressure, load = sample
    machine = machine_budget.values()  # The shared value follows machine.yml only.
    ceiling = machine["max_parallel"]
    with scheduler.machine_lock(ctx.store):
        state = scheduler.read_state(ctx.store)
        current = int(state["parallel"]) if state else scheduler.INITIAL_PARALLEL
        if state and ceiling is not None and current > ceiling:
            # A lower ceiling applies at once and alone: no AIMD step, no decrease
            # interval, no decreased_at. Running work keeps going; new starts see it.
            scheduler.write_state(ctx.store, ceiling, now, decreased_at=state.get("decreased_at"))
            scheduler.append_budget_history(
                ctx.store.root.parent, now, "parallel", current, ceiling, pressure=pressure,
                load_per_cpu=load, swapins=getattr(sample, "swapins", None), wanted=ceiling,
                load_average_per_cpu=getattr(sample, "load_average_per_cpu", None),
                cpu_busy=getattr(sample, "cpu_busy", None),
            )
            return max(1, min(ceiling, scheduler.machine_runs(ctx.store, now) + waiting))
        changed_at = scheduler.datetime.fromisoformat(state["changed_at"]) if state else now
        demand = scheduler.machine_runs(ctx.store, now) + waiting
        # Work only caps growth: the shared value never drops below itself for lack
        # of work here, so it stays the load-driven value. Memory never moves it (#497).
        wanted = scheduler.next_parallel(
            current, pressure="normal", load_per_cpu=load, lanes_with_work=max(demand, current),
            ceiling=ceiling, increase_ok=now - changed_at >= scheduler.INCREASE_INTERVAL,
        )
        if wanted < current and not scheduler.decrease_allowed(state, now):
            wanted = current
        value = wanted
        if state is None or value != current:
            decreased_at = (
                now.astimezone(scheduler.UTC).isoformat() if value < current else (state or {}).get("decreased_at")
            )
            scheduler.write_state(ctx.store, value, now, decreased_at=decreased_at)
        scheduler.append_budget_history(
            ctx.store.root.parent, now, "parallel", current, value,
            pressure=pressure, load_per_cpu=load, swapins=getattr(sample, "swapins", None), wanted=wanted,
            load_average_per_cpu=getattr(sample, "load_average_per_cpu", None),
            cpu_busy=getattr(sample, "cpu_busy", None),
        )
    return max(1, min(value, demand))


def _has_slot(ctx: TickContext, budget: int) -> bool:
    """Machine-wide runs stay under the shared budget; this project's under its ceiling."""
    if scheduler.machine_runs(ctx.store, ctx.now()) >= budget:
        return False
    return ctx.config.max_parallel is None or len(ctx.store.active_runs()) < ctx.config.max_parallel


def _reserve_slot(ctx: TickContext, number: int, budget: int) -> bool:
    """Recount and journal the start under the machine lock; GitHub work happens outside."""
    with scheduler.machine_lock(ctx.store):
        if not scheduler._has_slot(ctx, budget):
            return False
        ctx.store.prepare_run_start(number)
        return True


def _start_ready(
    ctx: TickContext, ready: dict[str, list[int]], lane_for: Callable[[Issue], str], base_ref: str,
    report: TickReport, sample: tuple[str, float] | None = None,
) -> bool | None:
    """Start ready lanes within the machine budget; None means a GitHub wait began."""
    def eligible(issue: Issue, lane: str) -> bool:
        return (
            issue.state == "open" and not issue.is_pull_request
            and {scheduler.APPROVED, scheduler.READY}.issubset(issue.labels)
            and not issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN, scheduler.RUNNING} and lane_for(issue) == lane
        )

    sample = sample if sample is not None else scheduler.machine_load()
    budget = scheduler._machine_budget(ctx, ready, sample)
    started = False
    for lane in sorted(ready, key=scheduler.lane_order):
        if not scheduler._has_slot(ctx, budget):
            break
        if _lane_held(ctx, lane, ctx.now()):
            continue
        for number in ready[lane]:
            local = scheduler._local_issue(ctx, number)
            if local is not None and not eligible(local, lane):
                continue
            issue = ctx.tracker.get_issue(number)
            if not eligible(issue, lane):
                continue
            if not scheduler._can_start_in_lane(ctx, lane, issue):
                continue
            approval = ctx.store.load_approval(number)
            event, _ = scheduler._latest_approval(ctx.tracker.label_events(number), ctx.config.approvers)
            if approval is None or event is None or event.created_at != approval.approved_at:
                continue
            if not scheduler._check_integrity(ctx, issue, approval, base_ref, report):
                continue
            if not scheduler._check_prerequisites(ctx, issue, approval, report):
                continue

            if not eligible(scheduler._local_issue(ctx, number) or issue, lane):
                continue
            if sample[0] == "critical":
                # This issue passed every start check; only critical memory holds it (#497).
                # Running work is left alone, and one row records the held tick.
                scheduler.append_memory_hold(
                    ctx.store.root.parent, ctx.now(), pressure=sample[0], load_per_cpu=sample[1],
                    swapins=getattr(sample, "swapins", None),
                    load_average_per_cpu=getattr(sample, "load_average_per_cpu", None),
                    cpu_busy=getattr(sample, "cpu_busy", None),
                )
                return False
            if not scheduler._reserve_slot(ctx, number, budget):
                return started
            try:
                ctx.tracker.remove_labels(number, [scheduler.READY])
                ctx.tracker.add_labels(number, [scheduler.RUNNING])
                scheduler.board.sync_status(ctx, number, "Running")
                if until := ctx.store.github_wait_until(ctx.now()):
                    report.skipped = f"GitHub rate limit until {until.isoformat()}"
                    return None
            except scheduler.TrackerError as exc:
                if not exc.is_rate_limit:
                    raise
                until = scheduler.orch_github.pause(ctx, exc)
                # A label may already say running even though no process started.
                # Record availability, so recovery does not charge a crash.
                ctx.store.start_run(
                    number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number),
                    lane=lane, tier=scheduler.issue_size(issue.labels),
                )
                ctx.store.clear_run_start(number)
                scheduler.orch_github.wait_run(ctx, number, until)
                ctx.store.finish_run(number, "limit-wait", detail=str(exc))
                report.skipped = f"GitHub rate limit until {until.isoformat()}"
                return None
            ctx.store.start_run(
                number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), lane=lane,
                tier=scheduler.issue_size(issue.labels),
            )
            ctx.store.clear_run_start(number)
            pid = ctx.launcher(number)
            ctx.store.update_run(number, pid=pid)
            report.actions.append(f"#{number} started running (pid={pid})")
            started = True
            if not scheduler._has_slot(ctx, budget):
                return started
    return started


def _reopen_view(ctx: TickContext, report: TickReport) -> None:
    with step("view_reopen"):
        try:
            scheduler.watch_open.reopen(ctx.repo_root, ctx.store, ctx.config.language, ctx.now())
        except Exception as exc:
            # Optional progress display must never hide successfully started runs.
            error = " ".join(str(exc).split())[:200]
            report.actions.append(scheduler.text("watch_open_failed", ctx.config.language, error=error))


def _github_wait(ctx: TickContext, ready: dict[str, list[int]], name: str) -> str | None:
    """One timed check of the GitHub pause; budgets still apply while it lasts."""
    with step(name):
        if until := ctx.store.github_wait_until(ctx.now()):
            scheduler._apply_budgets(ctx, ready)
            return f"GitHub rate limit until {until.isoformat()}"
    return None


def _github_pause(ctx: TickContext, exc: Exception) -> str:
    """Record a GitHub rate limit as one timed step and apply this tick's budgets."""
    with step("github_pause"):
        until = scheduler.orch_github.pause(ctx, exc)
        scheduler._apply_budgets(ctx, {})
    return f"GitHub rate limit until {until.isoformat()}"

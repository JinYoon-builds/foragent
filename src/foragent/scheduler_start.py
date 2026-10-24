"""Size the machine budget and start ready issues within it."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from foragent import board, orch_github, scheduler
from foragent.capacity import INCREASE_INTERVAL, INITIAL_PARALLEL, machine_lock, machine_runs, read_state, write_state
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_size, lane_order
from foragent.tracker import Issue, TrackerError

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

def _machine_budget(
    ctx: TickContext, ready: dict[str, list[int]], sample: tuple[str, float] | None = None,
) -> int:
    """Update the machine-wide AIMD value once per tick and return this tick's machine budget."""
    now = ctx.now()
    lane_runs = Counter(run.lane for run in ctx.store.active_runs())
    # A crowded lane may add a clone; the file-overlap check runs at start time.
    waiting = sum(
        1 for lane, numbers in ready.items()
        if scheduler._lane_has_room(lane_runs[lane], len(numbers)) and not ctx.store.lane_paused(lane, now)
    )
    pressure, load = sample if sample is not None else scheduler.machine_load()
    with machine_lock(ctx.store):
        state = read_state(ctx.store)
        current = int(state["parallel"]) if state else INITIAL_PARALLEL
        changed_at = datetime.fromisoformat(state["changed_at"]) if state else now
        demand = machine_runs(ctx.store, now) + waiting
        # Work only caps growth: the shared value never drops below itself for lack
        # of work here, so it stays the pressure- and load-driven value.
        value = scheduler.next_parallel(
            current, pressure=pressure, load_per_cpu=load, lanes_with_work=max(demand, current),
            ceiling=None, increase_ok=now - changed_at >= INCREASE_INTERVAL,
        )
        if state is None or value != current:
            write_state(ctx.store, value, now)
    return max(1, min(value, demand))


def _has_slot(ctx: TickContext, budget: int) -> bool:
    """Machine-wide runs stay under the shared budget; this project's under its ceiling."""
    if machine_runs(ctx.store, ctx.now()) >= budget:
        return False
    return ctx.config.max_parallel is None or len(ctx.store.active_runs()) < ctx.config.max_parallel


def _reserve_slot(ctx: TickContext, number: int, budget: int) -> bool:
    """Recount and journal the start under the machine lock; GitHub work happens outside."""
    with machine_lock(ctx.store):
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
            and {APPROVED, READY}.issubset(issue.labels)
            and not issue.labels & {BLOCKED, NEEDS_HUMAN, RUNNING} and lane_for(issue) == lane
        )

    budget = scheduler._machine_budget(ctx, ready, sample)
    started = False
    for lane in sorted(ready, key=lane_order):
        if not scheduler._has_slot(ctx, budget):
            break
        if ctx.store.lane_paused(lane, ctx.now()):
            continue
        for number in ready[lane]:
            local = scheduler._local_issue(ctx, number)
            if local is not None and not eligible(local, lane):
                continue
            issue = ctx.tracker.get_issue(number)
            if not eligible(issue, lane):
                continue
            if not scheduler._can_start_in_lane(ctx, lane, issue, len(ready[lane])):
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
            if not scheduler._reserve_slot(ctx, number, budget):
                return started
            try:
                ctx.tracker.remove_labels(number, [READY])
                ctx.tracker.add_labels(number, [RUNNING])
                board.sync_status(ctx, number, "Running")
                if until := ctx.store.github_wait_until(ctx.now()):
                    report.skipped = f"GitHub rate limit until {until.isoformat()}"
                    return None
            except TrackerError as exc:
                if not exc.is_rate_limit:
                    raise
                until = orch_github.pause(ctx, exc)
                # A label may already say running even though no process started.
                # Record availability, so recovery does not charge a crash.
                ctx.store.start_run(
                    number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number),
                    lane=lane, tier=issue_size(issue.labels),
                )
                ctx.store.clear_run_start(number)
                orch_github.wait_run(ctx, number, until)
                ctx.store.finish_run(number, "limit-wait", detail=str(exc))
                report.skipped = f"GitHub rate limit until {until.isoformat()}"
                return None
            ctx.store.start_run(
                number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), lane=lane,
                tier=issue_size(issue.labels),
            )
            ctx.store.clear_run_start(number)
            pid = ctx.launcher(number)
            ctx.store.update_run(number, pid=pid)
            report.actions.append(f"#{number} started running (pid={pid})")
            started = True
            break
    return started

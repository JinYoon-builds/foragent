"""Size the machine budget and start ready issues within it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from foragent import machine_budget, orch_plan, orch_queue, overlap_files, quota_events, resource_sample, scheduler, scheduler_quota
# The facade re-exports these capacity and baseline bindings from here; calls below go through it.
from foragent.capacity import (  # noqa: F401
    INCREASE_INTERVAL, INITIAL_PARALLEL, LAUNCH_GRACE, append_budget_history, append_memory_hold, decrease_allowed, machine_load,
    machine_lock, machine_runs, next_parallel, read_state, update_test_slots, write_state,
)
from foragent.issue import parse_body
from foragent.orch_tick_outcome import step, timed  # noqa: F401
from foragent.run_baseline import prebuild_acceptance_baseline, start_baseline_thread  # noqa: F401
from foragent.scheduler_quota import choose_executor, claude_allowed  # noqa: F401  # Re-exported by the facade (#107, #186).
from foragent.tracker import Issue

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _lane_held(ctx: TickContext, lane: str, now: datetime, number: int | None = None) -> bool:
    """A start waits while a live plan pauses its lane, or quota admission holds it.

    ``number`` makes the quota decision the issue's own (#786): a continued run keeps its
    implementer and skips keep_percent, so a lane-wide answer must not hold it back.
    A start switched to Codex (#107) skips a Claude-caused lane pause, leaves it in place,
    and waits only on its own pair's executor pauses.
    """
    if orch_plan.lane_paused(ctx.store, lane, now):
        return True
    if (dream := ctx.store.get_dream(lane)) is not None and dream.status == "running":
        return True  # While a lane dreams, no new issue of that lane starts.
    return scheduler_quota.decide(ctx.store, ctx.config, lane, now, number).held


def _at_plan_width(ctx: TickContext, lane: str) -> bool:
    """A live plan width caps the lane's running runs; without one the lane has no fixed cap."""
    width = orch_plan.width(ctx.store, lane, ctx.now())
    return width is not None and sum(run.lane == lane for run in ctx.store.active_runs()) >= width


def _apply_budgets(
    ctx: TickContext, ready: dict[str, list[int]], *, workers: bool = True,
) -> tuple[str, float]:
    """Apply machine.yml and CPU load to the shared budgets once per tick; return its sensor sample.

    Every tick runs this, a GitHub wait included. ``workers=False`` leaves the
    worker budget to the caller, which sizes it with this sample right away.
    """
    if (sample := getattr(ctx, "_budget_sample", None)) is not None:
        return sample  # Already applied in this tick.
    measured_at = ctx.now()  # Before the sensors: a later publication keeps the measured time (D93).
    sample, machine = scheduler.machine_load(), machine_budget.values()
    ctx._budget_sample = sample
    resource_sample.record(ctx.store, sample, measured_at)
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
    waiting = sum(1 for lane, numbers in ready.items() for number in numbers
                  if not _lane_held(ctx, lane, now, number))
    waiting += getattr(ctx, "_dream_demand", 0)  # A due dream asks for one worker too.
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
            ctx._tick_budget = max(1, min(ceiling, scheduler.machine_runs(ctx.store, now) + waiting))
            return ctx._tick_budget
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
    ctx._tick_budget = max(1, min(value, demand))
    return ctx._tick_budget


def _memory_holds(pressure: str) -> bool:
    """Whether memory pressure has reached `fa settings`' memory_pressure_limit (default critical)."""
    from foragent import settings

    limit = settings.read_machine().get("memory_pressure_limit", "critical")
    return pressure == "critical" or (limit == "warn" and pressure == "warn")


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


def _overlap_count(ctx: TickContext, number: int, claims: list) -> int:
    """How many of the issue's declared files overlap the files running runs hold."""
    if not claims or (approval := ctx.store.load_approval(number)) is None:
        return 0
    return sum(any(overlap_files.any_overlap(ctx, [path], claim.files) for claim in claims)
               for path in parse_body(approval.body).files)


def _ranked(ctx: TickContext, ready: dict[str, list[int]], claims: list | None = None) -> list[tuple[str, int]]:
    """Every lane's candidates in one start order; each still meets every start rule.

    Fewer declared files overlapping running runs first, then the cross-lane
    priority list, then the position in the lane's (already queue-ordered) list,
    then the issue number. ``claims`` lets a read-only view pass its recorded claims.
    """
    if claims is None:
        claims = overlap_files.claims(ctx)[0] if ctx.store.active_runs() else []
    priority = {number: index for index, number in enumerate(orch_queue.get_global(ctx.store))}
    unlisted = len(priority)
    keyed = [
        ((_overlap_count(ctx, number, claims), priority.get(number, unlisted), position, number), lane, number)
        for lane in ready for position, number in enumerate(ready[lane])
    ]
    return [(lane, number) for _, lane, number in sorted(keyed)]


def _start_checks(
    ctx: TickContext, number: int, lane: str, eligible: Callable[[Issue, str], bool], base_ref: str,
    report: TickReport,
) -> Issue | None:
    """Read the issue now and return it when every start check passes.

    After a failed refresh the read is one GitHub issue GET of at most
    GH_CALL_SECONDS with no retry, so the stored candidate never starts on
    stale labels, body or state; the usual approval and integrity checks follow.
    """
    if getattr(ctx, "_refresh_failed", False):
        with scheduler.gh.bounded(scheduler.GH_CALL_SECONDS, scheduler.GH_CALL_SECONDS):
            issue = ctx.tracker.get_issue(number)
    else:
        issue = ctx.tracker.get_issue(number)
    if (scope := getattr(ctx, "_scope", None)) is not None and not scope.owns(issue):
        return None  # Ownership is fixed by this tick's snapshot (#1004).
    if not eligible(issue, lane):
        return None
    if not scheduler._can_start_in_lane(ctx, lane, issue):
        return None
    approval = ctx.store.load_approval(number)
    event, _ = scheduler._latest_approval(ctx.tracker.label_events(number), ctx.config.approvers)
    if approval is None or event is None or event.created_at != approval.approved_at:
        return None
    if not scheduler._check_integrity(ctx, issue, approval, base_ref, report):
        return None
    if not scheduler._check_prerequisites(ctx, issue, approval, report):
        return None
    return issue


def _start_ready(
    ctx: TickContext, ready: dict[str, list[int]], lane_for: Callable[[Issue], str], base_ref: str,
    report: TickReport, sample: tuple[str, float] | None = None,
) -> bool | None:
    """Start ranked ready issues across lanes within the machine budget; None means a GitHub wait began."""
    def eligible(issue: Issue, lane: str) -> bool:
        return (
            issue.state == "open" and not issue.is_pull_request
            and {scheduler.APPROVED, scheduler.READY}.issubset(issue.labels)
            and not issue.labels & {scheduler.BLOCKED, scheduler.NEEDS_HUMAN, scheduler.RUNNING} and lane_for(issue) == lane
        )

    sample = sample if sample is not None else scheduler.machine_load()
    budget = scheduler._machine_budget(ctx, ready, sample)
    started = False
    if ctx.store.github_outbox():
        # Undelivered writes queue every later write, so READY/RUNNING would change only locally while
        # GitHub still shows the issue ready: a runner must not start on a queued write.
        report.actions.append(scheduler.text("tick_starts_wait_outbox", ctx.config.language))
        return started
    for lane, number in _ranked(ctx, ready):
        if not scheduler._has_slot(ctx, budget):
            break
        if _lane_held(ctx, lane, ctx.now(), number):
            continue
        if _at_plan_width(ctx, lane):
            continue
        local = scheduler._local_issue(ctx, number)
        if local is not None and not eligible(local, lane):
            continue
        try:
            issue = _start_checks(ctx, number, lane, eligible, base_ref, report)
        except scheduler.TrackerError as exc:
            # After a failed refresh, a candidate whose current facts cannot be read waits; others go on.
            if exc.is_rate_limit or not getattr(ctx, "_refresh_failed", False):
                raise
            report.actions.append(scheduler.text("tick_issue_deferred", ctx.config.language,
                                                 number=number, error=scheduler._one_line(exc)))
            continue
        if issue is None:
            continue

        if not eligible(scheduler._local_issue(ctx, number) or issue, lane):
            continue
        # Chosen once per start; a continued run keeps its stored implementer and waits on its own pair (#107).
        start = scheduler_quota.decide(ctx.store, ctx.config, lane, ctx.now(), number)
        if start.held:
            continue
        executor, reviewer = quota_events.pair(start.config)  # Stored once per start (#186).
        if _memory_holds(sample[0]):
            # This issue passed every start check; only memory at the machine's limit holds it (#497, #160).
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
            until = ctx.store.github_wait_until(ctx.now())  # A queued label write may have met the limit.
            detail = f"GitHub rate limit until {until.isoformat()}" if until else ""
        except scheduler.TrackerError as exc:
            if not exc.is_rate_limit:
                raise
            until, detail = scheduler.orch_github.pause(ctx, exc), str(exc)
        # Every start, also one a GitHub limit stops, stores its pair and journals its switch (#186).
        ctx.store.start_run(
            number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), lane=lane,
            executor=executor, reviewer=reviewer, executor_model=start.model,  # The runner's first turn writes the tier (#238).
            journal=scheduler_quota.start_journal(ctx.store, ctx.now(), number, start.switch,
                                                  ctx.config, lane, executor, reviewer),
        )
        if until:  # A label may already say running though no process started: availability, not a crash.
            ctx.store.clear_run_start(number)
            scheduler.orch_github.wait_run(ctx, number, until)
            ctx.store.finish_run(number, "limit-wait", detail=detail)
            report.skipped = f"GitHub rate limit until {until.isoformat()}"
            return None
        if start.switch is not None:  # Claude is held back: this run implements with Codex (#107).
            report.actions.append(scheduler_quota.switch_line(number, start.switch, ctx.config.language))
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
    ctx._push_ready = False  # #129: a tick that hits a rate limit after its wait check sends no push.
    with step("github_pause"):
        until = scheduler.orch_github.pause(ctx, exc)
        scheduler._apply_budgets(ctx, {})
    return f"GitHub rate limit until {until.isoformat()}"

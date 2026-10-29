"""The run runtime under the shared silence watchdog (#435).

The gate tick is the single writer of ``silence/N.json``: the watchdog
state of the current run, carried from one tick to the next. Output is the run
log, progress is ``last_progress_at`` and CPU comes from the tick's one process
snapshot over the run's recorded identities.
"""

from __future__ import annotations

from dataclasses import replace
import json
from typing import TYPE_CHECKING

from foragent import processes, scheduler
from foragent.watchdog import Decision, SilenceWatchdog

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport
    from foragent.state import Run


def _path(ctx: TickContext, number: int):
    return ctx.store.root / "silence" / f"{number}.json"


def observe(ctx: TickContext, run: Run) -> Decision:
    """This tick's decision; an unreadable table or CPU is unknown, never silence."""
    started = (scheduler.datetime.fromisoformat(run.started_at).timestamp()
               + run.waited_seconds + run.slept_seconds)
    watch = SilenceWatchdog(safety_net=ctx.config.safety_net_hours * 3600,
                            window_seconds=ctx.config.silence_minutes * 60, started=started)
    path = _path(ctx, run.number)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("run_started_at") == run.started_at:
            watch.restore(saved)
    except (OSError, ValueError, AttributeError):
        pass
    if run.last_progress_at and watch.last_activity_at is not None and watch.output_bytes is None:
        # The first observation starts silence at the run's last recorded progress.
        progressed = scheduler.datetime.fromisoformat(run.last_progress_at).timestamp()
        watch.last_output_at = watch.last_activity_at = max(watch.last_activity_at, progressed)
    watch.started = started  # Credited waits and machine sleep move the safety net, as the old limit.
    snapshot = processes.current_snapshot()
    rows = snapshot.rows.values() if snapshot is not None and snapshot.available else None
    log = ctx.store.log_path(run.number)
    progress = run.last_progress_at
    decision = watch.observe(
        now=ctx.now().timestamp(), output_bytes=log.stat().st_size if log.exists() else 0,
        progress_events=int(scheduler.datetime.fromisoformat(progress).timestamp() * 1000) if progress else 0,
        snapshot=rows, identities=run.process_identities,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx.store._atomic_write(path, json.dumps({"run_started_at": run.started_at, **watch.state()}) + "\n")
    return decision


def _log(ctx: TickContext, number: int, decision: Decision, kind: str, summary: str) -> None:
    scheduler.append_event(ctx.store, ctx.now(), number, kind, summary, **decision.facts())


def stop(ctx: TickContext, run: Run, report: TickReport, decision: Decision,
         past_limit: bool, wake_grace: bool) -> bool:
    """Stop the run when the watchdog says so; True when this handled the run.

    Each decision is journaled with its facts once its stop succeeded: a refused
    stop is reported as stop-refused, and the next tick decides again.
    """
    if decision.decision == "safety-net":
        # Still progressing at the net: end it like a stall so its WIP is kept and resumed.
        _stalled(ctx, run, report, decision, "safety-net", "Runner reached the safety net; its WIP is kept.")
        return True
    if past_limit and not decision.grew:
        if decision.decision == "running":
            decision = replace(decision, stop=True, decision="runtime-limit")
        if scheduler._kill_run(ctx, run.number, report, detail="Runner exceeded its runtime limit."):
            _log(ctx, run.number, decision, "runtime-limit",
                 "Run passed max_runtime_minutes with no growing output, CPU or progress.")
            scheduler.flag(ctx, run.number, scheduler.Reason.TIMEOUT, "Too big for one session; split it in order")
            report.actions.append(f"#{run.number} flagged timeout")
        return True
    if decision.decision == "silent" and not wake_grace:  # The watchdog never calls unknown CPU silent.
        with processes.quiescent_tree():
            _stalled(ctx, run, report, decision, "silence-stop",
                     "Runner output, progress and process-tree CPU were silent for the whole window.")
        return True
    return False


def _stalled(ctx: TickContext, run: Run, report: TickReport, decision: Decision, kind: str, detail: str) -> None:
    """A silence or safety-net stop always ends as stalled-restart and returns to ready.

    The next run resumes the kept WIP. Neither stop spends the stall restart budget:
    that budget belongs to stall_minutes, which stays a separate stop.
    """
    # The tick killed the whole tree under its lock, as `fa orch stop` does: its deletions are the run's.
    if scheduler._kill_run(ctx, run.number, report, outcome="stalled-restart", count_stall=True,
                           count_restart=False, detail=detail, owned_deletions=True):
        _log(ctx, run.number, decision, kind, detail)
        scheduler._restore_stalled(ctx, run.number, 1, report)

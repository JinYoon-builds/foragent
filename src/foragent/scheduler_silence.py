"""The run check of the gate tick under the shared watchdog (#435).

The samples live in the run record (``Run.silence``), written by this tick alone; output
is the run log, progress is ``last_progress_at`` and CPU comes from the tick's one process
snapshot over the run's recorded identities. ``watchdog.decide`` decides and
``watchdog.outcome_for`` maps; this module only carries the result out. Dream runs keep
their current runtime limit (out of scope). Kept apart from scheduler_recovery only for
its module-size limit.
"""

from __future__ import annotations

from dataclasses import replace
import json
from typing import TYPE_CHECKING

from foragent import processes, scheduler
from foragent.watchdog import Decision, SilenceWatchdog, outcome_for, runtime_limit_seconds, silence_window

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport
    from foragent.state import Run


def check(ctx: TickContext, run: Run, report: TickReport, runtime: float) -> bool:
    """Observe and stop the run when the watchdog says so; True when this handled the run."""
    if run.kind != "issue":
        return _dream_limit(ctx, run, report, runtime)
    decision, run = observe(ctx, run, runtime)
    outcome = outcome_for(decision.kind)
    if outcome is None:
        return False
    log = ctx.store.log_path(run.number)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:  # Logged when decided, whatever the stop does next.
        stream.write(json.dumps(decision.facts()) + "\n")
    # One snapshot with identity rechecks; children forked after it are left to the reaper.
    with processes.quiescent_tree():
        if outcome == "killed":
            if scheduler._kill_run(ctx, run.key, report, outcome="killed", unless_talking=True,
                                   detail="Runner exceeded its runtime limit and was silent.", decided=run):
                scheduler.flag(ctx, run.number, scheduler.Reason.TIMEOUT, "Too big for one session; split it in order")
                report.actions.append(f"#{run.number} flagged timeout")
            return True
        # Like a stall: it adds to restarts, and past the restart limit the run is blocked as stalled.
        restarts = run.restarts + 1
        if scheduler._kill_run(ctx, run.key, report, outcome="stalled-restart" if restarts <= 1 else "stalled",
                               count_stall=True, decided=run, unless_talking=True, owned_deletions=True,
                               detail=f"Runner stopped by the watchdog ({decision.kind}); its WIP is kept."):
            scheduler._restore_stalled(ctx, run.number, restarts, report)
    return True


def observe(ctx: TickContext, run: Run, runtime: float) -> tuple[Decision, Run]:
    """This tick's decision, from the samples the run record keeps; the record is updated first."""
    now = ctx.now().timestamp()
    started = now - runtime  # The run start, moved by credited waits, sleep and talk pauses.
    window, source = silence_window(ctx.config)
    watch = SilenceWatchdog(safety_net=ctx.config.safety_net_hours * 3600,
                            window_seconds=None if source == "default" else window, started=started,
                            runtime_limit=runtime_limit_seconds(ctx.config))
    watch.restore(run.silence)
    if run.last_progress_at:  # Progress is a measured activity time, also with no earlier sample.
        progressed = scheduler.datetime.fromisoformat(run.last_progress_at).timestamp()
        watch.last_output_at = max(watch.last_output_at, progressed)
        watch.last_activity_at = max(watch.last_activity_at, progressed)
    log = ctx.store.log_path(run.number)
    decision = watch.observe(now=now, output_bytes=log.stat().st_size if log.exists() else 0, progress_events=0,
                             snapshot=_rows(), identities=run.process_identities)
    ctx.store.update_run(run.key, silence=watch.state())
    # The decision was taken from this record: progress written since must void it in _kill_run.
    return decision, replace(run, silence=watch.state())


def _rows():
    """The tick's one snapshot; outside a tick there is none to reuse, so the table is read once.
    None when the table cannot be read: CPU is then unknown and only the safety net stops."""
    snapshot = processes.current_snapshot()
    if snapshot is not None:
        return snapshot if snapshot.available else None
    try:
        return processes.list_processes()
    except OSError:
        return None


def _dream_limit(ctx: TickContext, run: Run, report: TickReport, runtime: float) -> bool:
    """A dream keeps main's fixed runtime limit."""
    if runtime <= runtime_limit_seconds(ctx.config):
        return False
    scheduler._kill_run(ctx, run.key, report, outcome="error", unless_talking=True,
                        detail="Runner exceeded its runtime limit.", decided=run)
    return True

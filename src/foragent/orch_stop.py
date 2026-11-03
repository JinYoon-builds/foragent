"""`fa orch stop N --keep-work --why`: stop a running issue through the stall-kill path.

The command takes the tick lock, so it is the only kill writer besides the tick,
checks #261's rules on the recorded PIDs without signaling, and calls
scheduler._kill_run(outcome="stopped"): the existing path terminates the tree and
saves the WIP (named `[stopped: <why>]`). _restore_stopped then returns the issue
to ready with one stop event. Every unfinished record `fa gate status` lists is
stoppable; when its workers are already gone the same path saves the work (#1207).

`fa orch stop --all --keep-work --why` (#1491) is the project-wide stop, and
`fa orch release` is its pair. Both append to `<state>/stop-requests.jsonl` (their
only writers); `active()` derives the stop in force from it and is the admission every
start asks (tick starts, dreams, drafts, role sessions, review reruns, run entry). The
stop also holds each lane not held yet through the plan's own pause (orch_plan stays the
writer of lane holds); the release lifts only those holds. It signals nothing: a running
run's owner stops after its current agent turn (`raise_if_requested`) and saves its
work, and `GitRepo.remove_worktree` never discards a dirty worktree while a stop is in
force.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from typing import TYPE_CHECKING

from foragent import processes
from foragent.i18n import text
from foragent.labels import lane_order
from foragent.run_attempt import ExecutorStopped
from foragent.state import Run

if TYPE_CHECKING:
    from pathlib import Path

    from foragent.runner import RunContext
    from foragent.state import StateStore

TICK_LOCK_WAIT_S = 60
STOP_PREFIX = "project stop: "


@contextmanager
def _tick_lock(ctx: RunContext, n: int | None) -> Iterator[None]:
    """Hold the gate's tick lock, polling for up to TICK_LOCK_WAIT_S while a tick holds it."""
    from foragent.orch import OrchError

    deadline = ctx.monotonic() + TICK_LOCK_WAIT_S
    while True:
        with ctx.store.hold_tick_lock() as held:
            if held:
                yield
                return
        if ctx.monotonic() >= deadline:
            if n is None:
                raise OrchError(text("orch_stop_all_busy", ctx.config.language))
            _note(ctx, n, None, "skipped", "busy")
            raise OrchError(text("orch_stop_busy", ctx.config.language, n=n))
        ctx.sleep(1)


def unsafe_target(run: Run) -> tuple[str, int, str] | None:
    """The first recorded PID #261 forbids signaling, as (role, pid, rule); read without any signal.

    The rules are processes._protected()'s lineage, group and session, PID 1 or
    lower, and a start time that differs from the recorded identity. A PID with
    no recorded identity, or an unreadable process list, is left to _kill_run,
    which applies #261's own checks.
    """
    try:
        rows = {row.pid: row for row in processes.list_processes()}
    except (OSError, PermissionError):
        return None
    lineage, group, session = processes._protected(rows)
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    for role, pid in (("runner", run.pid), ("executor", run.executor_pgid)):
        if pid is None:
            continue
        row = rows.get(pid)
        if pid <= 1 or pid in lineage:
            return role, pid, "this command's own lineage" if pid > 1 else "PID 1 or lower"
        if row is None:
            continue
        if row.pgid in lineage or row.sid in lineage or row.pgid == group or row.sid == session:
            return role, pid, "this command's process group or session"
        if pid in identities and identities[pid] != row.start_time:
            return role, pid, "a different process now has this PID"
    return None


def _note(ctx: RunContext, n: int, run: Run | None, decision: str, reason: str, detail: str | None = None) -> None:
    """An issue stop's decision; repeating the latest one is no new record, across executions too."""
    ctx.store.record_flow_change("stop", issue=n, run_started_at=run.started_at if run is not None else None,
                                 across_runs=True, decision=decision, reason=reason, detail=detail)


def _note_not_stopped(ctx: RunContext, tick_ctx, n: int, report) -> None:
    """Why the kill path returned without stopping; a refusal it already recorded is not said again."""
    from foragent.scheduler_stop import _stop_already_refused

    try:
        run = ctx.store.get_run(n)
        running = run is not None and run.status == "running"
        if running and _stop_already_refused(tick_ctx, run):
            return
        if report.actions:
            _note(ctx, n, run, "skipped", "failed", "; ".join(report.actions))
        else:
            _note(ctx, n, run, "skipped", "run-changed" if running else "not-running")
    except Exception:  # noqa: BLE001 - an observation never replaces the stop's own error
        return


def stop(ctx: RunContext, n: int, why: str) -> str:
    """Stop issue N's run through scheduler._kill_run and return it to ready, keeping its work."""
    from foragent import scheduler
    from foragent.orch import OrchError
    from foragent.scheduler_stop import _restore_stopped, unfinished_runs

    language = ctx.config.language
    why = " ".join(why.split())
    if not why:
        _note(ctx, n, None, "skipped", "why-required")
        raise OrchError(text("orch_stop_why_required", language))
    with _tick_lock(ctx, n):
        # The fact `fa gate status` lists as active_runs: every unfinished issue record,
        # with or without live workers (#1207). Process liveness only picks the kill path.
        run = next((run for run in unfinished_runs(ctx.store) if run.number == n), None)
        if run is None:
            _note(ctx, n, None, "skipped", "not-running")
            raise OrchError(text("orch_stop_not_running", language, n=n))
        if (unsafe := unsafe_target(run)) is not None:
            role, pid, rule = unsafe
            _note(ctx, n, run, "skipped", "unsafe", f"{role} {pid}: {rule}")
            raise OrchError(text("orch_stop_unsafe", language, n=n, role=role, pid=pid, rule=rule))
        tick_ctx = scheduler.TickContext(
            repo=ctx.repo, repo_root=ctx.repo_root, tracker=ctx.tracker, store=ctx.store,
            config=ctx.config, git=ctx.git, now=ctx.now, launcher=lambda number: None,
        )
        report = scheduler.TickReport()
        try:
            killed = scheduler._kill_run(tick_ctx, n, report, outcome="stopped", detail=why)
        except Exception as exc:
            _note(ctx, n, run, "skipped", "failed", f"{type(exc).__name__}: {exc}")
            raise
        if not killed:
            # The kill path's own changes (a refused stop, a dead run's recovery) stand.
            _note_not_stopped(ctx, tick_ctx, n, report)
            if not report.actions:
                raise OrchError(text("orch_stop_not_running", language, n=n))
            raise OrchError(text("orch_stop_failed", language, n=n, actions="; ".join(report.actions)))
        _note(ctx, n, run, "stopped", "requested", why)  # The kill stands even if a later restore step fails.
        _restore_stopped(tick_ctx, n, why, report)
    return text("orch_stop_done", language, n=n, why=why)  # _restore_stopped posted it as the audit comment.


def _requests(store: StateStore) -> Path:
    return store.root / "stop-requests.jsonl"


def _records(store: StateStore) -> list[dict]:
    """The stop and release records in order; a torn line is no record."""
    try:
        lines = _requests(store).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("why"), str):
            records.append(item)
    return records


def _since_release(records: list[dict]) -> list[dict]:
    """The stop records after the latest release; empty when no project stop is active."""
    tail = []
    for item in records:
        tail = [] if item.get("kind") == "release" else [*tail, item]
    return tail


def active(store: StateStore) -> str | None:
    """The why of the project-wide stop in force, or None; the one admission check every start asks.

    A stop stays in force until `fa orch release` records its pair; it never expires.
    """
    stops = _since_release(_records(store))
    return stops[-1]["why"] if stops else None


def raise_if_requested(ctx: RunContext, n: int) -> None:
    """The run owner's check before and after each external step: stop with outcome "stopped".

    The owner's existing preservation path then saves the worktree on the issue branch and
    finishes the run; the next tick's orphan recovery returns it to ready (_restore_stopped).
    """
    if (why := active(ctx.store)) is None:
        return
    run = ctx.store.get_run(n)
    if run is None or run.status != "running":
        return
    _note(ctx, n, run, "stopped", "project-stop", why)
    raise ExecutorStopped("stopped", why)


def stop_all(ctx: RunContext, why: str) -> str:
    """Record one project-wide stop and hold every lane not held yet; each run's owner stops its run.

    Nothing here signals a process or writes a run: the runner that owns each run reads the
    stop at its next step (`raise_if_requested`) and saves and finishes the run itself.
    The record names the lanes this stop held, so its release lifts only those.
    """
    from foragent import orch_plan
    from foragent.orch import OrchError
    from foragent.scheduler_stop import unfinished_runs

    language = ctx.config.language
    why = " ".join(why.split())
    if not why:
        raise OrchError(text("orch_stop_why_required", language))
    with _tick_lock(ctx, None):
        now = ctx.now()
        lanes = sorted({"main", *ctx.config.lanes}, key=lane_order)
        paused = orch_plan.load(ctx.store, now)["paused"]
        held = [lane for lane in lanes if lane not in paused]  # A lane held before keeps its own hold.
        for lane in held:
            orch_plan.pause(ctx.store, lane, orch_plan.UNTIL_RESUMED, f"{STOP_PREFIX}{why}")
        _append(ctx.store, {"at": now.isoformat(), "kind": "stop", "why": why, "held": held})
        running = sorted(run.number for run in unfinished_runs(ctx.store))
    lines = [text("orch_stop_all_held", language, lanes=", ".join(lanes), why=why)]
    lines += [text("orch_stop_all_owner", language, n=n) for n in running]
    return "\n".join(lines)


def release(ctx: RunContext, why: str = "") -> str:
    """Lift the project-wide stop and resume the lanes it held; a lane held before stays held.

    Works with no argument, so one command restarts FA; a given why is also kept in the journal.
    """
    from foragent import orch_plan
    from foragent.orch_events import append_event

    language = ctx.config.language
    why = " ".join(why.split())
    with _tick_lock(ctx, None):
        stops = _since_release(_records(ctx.store))
        if not stops:
            return text("orch_release_none", language)
        now = ctx.now()
        paused = orch_plan.load(ctx.store, now)["paused"]
        resumed = []
        for lane in dict.fromkeys(lane for item in stops for lane in item.get("held") or []):
            item = paused.get(lane)
            # Only the stop's own hold is lifted; a pause a person set since then stays.
            if (item is not None and item["until"] == orch_plan.UNTIL_RESUMED.isoformat()
                    and orch_plan.resume(ctx.store, lane)):
                resumed.append(lane)
        _append(ctx.store, {"at": now.isoformat(), "kind": "release", "why": why, "resumed": resumed})
        if why:
            append_event(ctx.store, now, None, "release", f"Project stop released: {why}", why=why, resumed=resumed)
    return text("orch_release_done", language, lanes=", ".join(resumed) or "-")


def _append(store: StateStore, record: dict) -> None:
    """Append one stop or release record; these commands are the file's only writers."""
    with store._state_lock(), _requests(store).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

"""`fa orch stop`'s durable record, completion and record-driven recovery.

Kept beside the scheduler's stop path (scheduler_stop) and used by both the
orch verb and the tick, so a stop is finished from its local record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import processes, scheduler
from foragent.scheduler_stop import _stop_run_processes
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def stop_summary(number: int, why: str) -> str:
    return f"#{number}: stopped and kept unfinished work on foragent/issue-{number}; why: {why}"


def _pending(store, number: int) -> Path:
    return store.root / "stops" / f"{number}.json"


def mark_stop_pending(store, run: Run, why: str) -> None:
    """Durably record a stop before its intent, signal or labels; only completion removes it.

    Recovery reads this record, never a label an earlier step (the stop's own, or
    a dying runner's flag) may already have removed.
    """
    path = _pending(store, run.number)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._atomic_write(path, json.dumps({"number": run.number, "started_at": run.started_at, "why": why}))


def ensure_stop_pending(store, run: Run, issue):
    """Keep a finished run's stop driven by its record, recreating one a race may have lost."""
    from foragent.run_publish import WIP_NOT_SAVED, stop_intent_lock

    with stop_intent_lock(store, run.number):
        current = store.get_run(run.number) or run
        if current.stop_why and current.status == "finished" and current.outcome != WIP_NOT_SAVED \
                and not _pending(store, run.number).exists():
            mark_stop_pending(store, current, current.stop_why)
    return issue


def record_stop_save(store, number: int, status: str) -> None:
    """Keep how a save under the request ended ("saved"/"clean") in the pending record.

    Called under the run's intent lock. With no pending record there is no active
    request whose evidence this could be.
    """
    path = _pending(store, number)
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("saved") == "saved":
            status = "saved"  # A later retry finding nothing left does not erase the WIP it made.
        store._atomic_write(path, json.dumps({**record, "saved": status}))


def stop_save_of(store, number: int) -> str | None:
    path = _pending(store, number)
    return json.loads(path.read_text(encoding="utf-8")).get("saved") if path.exists() else None


def drop_stop_pending(store, number: int) -> None:
    _pending(store, number).unlink(missing_ok=True)


class StopFailed(Exception):
    """The stop ended the run but cannot claim its work was kept under its why; the issue is flagged."""


def complete_stop(ctx: TickContext, run: Run) -> str:
    """Finish an issue whose run `fa orch stop` ended; returns a summary note.

    The stop event is required, so it is journaled first and once per run. Each
    label step is separate and idempotent, and the pending record is removed only
    after all of them succeed, so any failed step is repeated by the next tick.
    As `fa orch retry`, the issue returns to ready: a hold or this issue's own
    open PR (which a tick would otherwise read as review) is cleared, and the
    next run resumes from the issue branch. A closed issue only loses running.
    """
    from foragent.run_publish import keep_unsaved_work, stop_kept_work

    number = run.number
    if run.outcome != "stopped":
        # The finished record is final (finish_run never revises it): a runner that ended on
        # its own path after the request, such as one from before an upgrade, is not this stop.
        scheduler.flag(ctx, number, scheduler.Reason.RETRIES_EXCEEDED,
                       f"Stop requested, but the run finished as {run.outcome}.", f"Stop reason: {run.stop_why}")
        drop_stop_pending(ctx.store, number)
        raise StopFailed(f"#{number}: the run finished as {run.outcome}, not as stopped; the issue is blocked")
    _ensure_terminated(ctx, run)
    # A runner the stop interrupted left its worktree to be saved only now, after the
    # whole tree exited, so the evidence observed here is final.
    # A finished record is never revised, so this save's own result is the evidence; the
    # worktree goes only after the save positively ended as "saved" or "clean".
    worktree = Path(run.worktree)
    if worktree.exists():
        try:
            scheduler._preserve_failed_implementation(
                number, ctx, ctx.git.at(worktree), "stopped", approval=ctx.store.load_approval(number),
                tracker_available=ctx.store.github_wait_until(ctx.now()) is None, tree_exited=True, strict=True,
            )
        except Exception as exc:
            keep_unsaved_work(number, ctx, worktree, exc)  # Flags the issue; the worktree stays.
            drop_stop_pending(ctx.store, number)
            raise StopFailed(f"#{number}: the unfinished work could not be saved; its worktree is kept") from exc
        if stop_save_of(ctx.store, number) in {"saved", "clean"}:  # Durable before the removal.
            ctx.git.remove_worktree(worktree)
    elif stop_save_of(ctx.store, number) is None:
        _prove_nothing_to_save(ctx, run)
    if not stop_kept_work(ctx.git, run, stop_save_of(ctx.store, number)):
        # No WIP names the why (for example, a runner from before an upgrade saved its own).
        scheduler.flag(ctx, number, scheduler.Reason.RETRIES_EXCEEDED,
                       "Stopped, but the unfinished work was not saved under the stop reason.",
                       f"Stop reason: {run.stop_why}")
        drop_stop_pending(ctx.store, number)
        raise StopFailed(f"#{number}: no WIP commit names the stop reason; the issue is blocked for review")
    journal = ctx.store.root / "orch-events.jsonl"
    started = scheduler.datetime.fromisoformat(run.started_at)
    recorded = journal.exists() and any(
        event.get("issue") == number and event.get("kind") == "stop"
        and scheduler.datetime.fromisoformat(event["at"]) >= started
        for event in map(json.loads, journal.read_text(encoding="utf-8").splitlines())
    )
    if not recorded:
        scheduler.append_event(ctx.store, ctx.now(), number, "stop", stop_summary(number, run.stop_why))
    issue = ctx.tracker.get_issue(number)
    note, removals = "; the issue is closed, so it was not returned to ready", [scheduler.RUNNING]
    if issue.state == "open":
        note = ""
        if (pr := ctx.tracker.find_pr(f"foragent/issue-{number}")) is not None:
            ctx.tracker.close_pr(pr, f"Closed by Foragent orchestrator stop; the next run resumes from the issue "
                                     f"branch. Why: {run.stop_why}")
            note = f"; closed PR #{pr} so the next run resumes from the branch"
        ctx.tracker.add_labels(number, [scheduler.READY])
        removals = [scheduler.BLOCKED, scheduler.NEEDS_HUMAN, scheduler.RUNNING]
    for label in removals:  # One label per call: a failure leaves the rest for the retry.
        ctx.tracker.remove_labels(number, [label])
    if issue.state == "open":
        scheduler.board.sync_status(ctx, number, "Ready")
    drop_stop_pending(ctx.store, number)
    return note


def recorded_tree_alive(run: Run) -> bool:
    """Whether any recorded identity may still live, judged by identity, never by a bare number.

    A row counts only when both its PID and its start time match a recorded
    identity: a reused PID is another process (the safe killer ignores it too).
    An unreadable recorded PID of this user (or of an unknown owner) is
    unknown, which is never proof of death. A
    table that cannot be read at all raises, so completion stays pending.
    """
    table = processes.list_processes()
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    unreadable = getattr(table, "unreadable", frozenset())
    owners, own = getattr(table, "unreadable_uids", {}), processes.os.getuid()
    # Foragent's processes run as this user: an unreadable PID owned by another user is a reuse.
    return any(pid in unreadable and owners.get(pid, own) == own for pid in identities) or any(
        identities.get(row.pid) == row.start_time for row in table)


def _prove_nothing_to_save(ctx: TickContext, run: Run) -> None:
    """Record "clean" for a stop whose run never had implementer work to save.

    Lifecycle paths end without a save: a stop before the worktree existed, or a
    review-only run or a run_issue that removed its worktree in normal cleanup
    before any implementer turn. Only an implementer turn edits a worktree, so no
    turn, no worktree and a branch still at the request's head together prove
    nothing was lost. Any turn without a recorded save stays unproven.
    """
    from foragent.run_publish import stop_head, stop_intent_lock

    with stop_intent_lock(ctx.store, run.number):
        current = ctx.store.get_run(run.number) or run
        if current.turns == 0 and not Path(current.worktree).exists() \
                and stop_head(ctx.git, run.number) == current.stop_head:
            record_stop_save(ctx.store, run.number, "clean")


def tree_proven_gone(run: Run | None) -> bool:
    """Positive proof that no recorded process may still live; an unreadable table is no proof."""
    if run is None:
        return True
    try:
        return not recorded_tree_alive(run)
    except PermissionError:
        return False


def _ensure_terminated(ctx: TickContext, run: Run) -> None:
    """A recorded intent, or even a finished `stopped` record, is not proof the run's tree is gone.

    Recorded descendants still alive are stopped under #261's rules first; if they
    cannot be, completion raises and the pending record keeps the stop for a retry.
    """
    if not recorded_tree_alive(run):
        return
    report = scheduler.TickReport()
    if _stop_run_processes(ctx, run, report) != "refused" and not recorded_tree_alive(run):
        return
    raise RuntimeError("the run's recorded processes are still alive: " + ("; ".join(report.actions) or "stop refused"))


def recover_pending_stops(ctx: TickContext, report: TickReport) -> None:
    """Drive every recorded stop to its end from the local record, whatever the labels say.

    No stop runs while a tick holds the lock. A recorded stop wins over however
    the runner finished (children die first, so it may end on an ordinary path),
    except a merge or lost work. A run still recorded as running is left to
    _recover_runs.
    """
    from foragent.run_publish import WIP_NOT_SAVED, stop_intent_lock

    directory = ctx.store.root / "stops"
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        record = json.loads(path.read_text(encoding="utf-8"))
        number = record["number"]
        # Read, decide and delete under the lock the runner's finish takes: a finish
        # cannot land between this read and a deletion it would have changed.
        with stop_intent_lock(ctx.store, number):
            run = ctx.store.get_run(number)
            if run is None or run.started_at != record["started_at"]:
                drop_stop_pending(ctx.store, number)  # A newer run replaced the stopped one.
                continue
            if run.status == "running":
                continue  # _recover_runs: a live runner's abandoned intent is cleared, a dead one finishes.
            if run.outcome in {"merged", WIP_NOT_SAVED}:
                drop_stop_pending(ctx.store, number)  # The merge or the lost-work flag owns the issue.
                continue
        # Finished: its runner can no longer write, so completion runs outside the lock.
        try:
            report.actions.append(f"#{number} completed a pending stop{complete_stop(ctx, run)}")
        except StopFailed as exc:
            report.actions.append(f"#{number} stop failed: {exc}")
        except Exception as exc:
            report.actions.append(f"#{number} stop still pending: {' '.join(str(exc).split())}")


def clear_stale_stop(ctx: TickContext, run: Run) -> Run:
    """Drop a stop intent left on a live run by a `fa orch stop` that died before signaling.

    The stop holds the tick lock from its intent to its labels, so a tick that
    sees an intent on a live runner knows no stop is in progress. Left in place,
    the intent would silence the runner's own flags and end it as stopped.
    """
    if not run.stop_why:
        return run
    from foragent.run_publish import STOP_REQUEST_CLEARED, STOPPED, stop_intent_lock

    # Re-read under the lock the runner's finish takes: if the run finished (as the stop)
    # since the caller looked, its pending record stays for recover_pending_stops to
    # save the deferred work and return the issue to ready.
    with stop_intent_lock(ctx.store, run.number):
        current = ctx.store.get_run(run.number)
        if current is None or current.status != "running" or not current.stop_why:
            return current or run
        drop_stop_pending(ctx.store, run.number)
        cleared = ctx.store.update_run(run.number, **STOP_REQUEST_CLEARED,
                                       **({"outcome": None} if current.outcome == STOPPED else {}))
    scheduler.append_event(ctx.store, ctx.now(), run.number, "stop-abandoned",
                           f"#{run.number}: a stop ended before signaling the runner; why: {run.stop_why}")
    return cleared

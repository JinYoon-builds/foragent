"""Merge turn in join order within priority over append-only ``<state>/merge-queue.jsonl``.

State ownership: each run appends only its own ``ready``/``withdrawn`` entries,
only the turn holder appends ``turn`` records, and the file only grows. The
turn itself is derived on every read, never stored. Each target branch uses
the current cross-lane priority list, with join order within equal priority.
Listed issues precede all unlisted issues, which share one priority.

Reading the queue: a run's entry is keyed by issue and the run's started_at.
Its first ``ready`` entry fixes its position (a later head change keeps it). A
``withdrawn`` entry ends that position; a later ``ready`` joins at the tail. A
``turn`` record is history only. A run that merged leaves no withdrawn entry.
The head, which holds the turn, is the first live entry in priority order.
Priority changes can change the next turn, while the merge lock serializes
actual merges already in flight. Entries of runs that are not running are
skipped, never deleted; the scheduler's crash recovery decides that an owner
is dead.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, timedelta
import json
from time import monotonic
from typing import TYPE_CHECKING, NoReturn
from uuid import uuid4

from foragent import orch_queue
from foragent.run_waits import WaitPhase

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.state import StateStore

QUEUE = "merge-queue.jsonl"
MERGE_WAIT_SECONDS = 60 * 60
POLL_SECONDS = 30


@dataclass(frozen=True)
class Seat:
    """One run's identity in one target branch's merge queue."""

    n: int
    started_at: str | None
    target: str

    @property
    def key(self) -> tuple[int, str | None]:
        return self.n, self.started_at


def seat(ctx: RunContext, n: int, target: str) -> Seat:
    run = ctx.store.get_run(n)
    return Seat(n, run.started_at if run is not None else None, target)


def _rows(store: StateStore) -> list[dict]:
    try:
        raw = (store.root / QUEUE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("n"), int):
            rows.append(row)
    return rows


def _waiting(rows: list[dict], target: str) -> dict[tuple[int, str | None], dict]:
    """Entries still holding a queue position for ``target``, in queue order."""
    active: dict[tuple[int, str | None], dict] = {}
    for row in rows:
        if row.get("target", target) != target:  # Legacy rows name no target.
            continue
        key = (row["n"], row.get("started_at"))
        event = row.get("event")
        if event == "ready":
            active.setdefault(key, row)
        elif event == "withdrawn":
            active.pop(key, None)
    return active


def _live(store: StateStore, key: tuple[int, str | None]) -> bool:
    run = store.get_run(key[0])
    return run is not None and run.status == "running" and key[1] in {None, run.started_at}


def _turn_order(entries: dict[tuple[int, str | None], dict], priority: list[int]) -> list[tuple[int, str | None]]:
    """Stable priority order over entries already in join order."""
    ranks = {n: index for index, n in enumerate(priority)}
    return sorted(entries, key=lambda key: ranks.get(key[0], len(priority)))


def _append(store: StateStore, row: dict) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / QUEUE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")


def _row(ctx: RunContext, place: Seat, event: str, **fields) -> dict:
    return {"n": place.n, "event": event, "started_at": place.started_at, "target": place.target,
            **fields, "at": ctx.now().isoformat()}


def join(ctx: RunContext, place: Seat, head_sha: str) -> None:
    """Append this run's ready entry unless it already holds a position."""
    with ctx.store._state_lock():
        if place.key not in _waiting(_rows(ctx.store), place.target):
            _append(ctx.store, _row(ctx, place, "ready", head_sha=head_sha))


def withdraw(ctx: RunContext, place: Seat) -> None:
    """Leave the queue without merging; a later ready entry joins at the tail."""
    with ctx.store._state_lock():
        if place.key in _waiting(_rows(ctx.store), place.target):
            _append(ctx.store, _row(ctx, place, "withdrawn"))


def holds_turn(store: StateStore, place: Seat) -> bool:
    entries = _waiting(_rows(store), place.target)
    for key in _turn_order(entries, orch_queue.get_global(store)):
        if _live(store, key):
            return key == place.key
    return False


def record_turn(ctx: RunContext, place: Seat, base_sha: str, result: str) -> None:
    """Only the turn holder writes this: one record per merge try."""
    with ctx.store._state_lock():
        _append(ctx.store, _row(ctx, place, "turn", members=[place.n], base_sha=base_sha, result=result))


@contextmanager
def _merge_wait(n: int, ctx: RunContext):
    """Credit one merge queue wait and keep its liveness visible to tick."""
    started = ctx.now()
    started_mono = ctx.monotonic()
    started_real = monotonic()
    previous_wait = ctx.store.get_run(n).merge_waited_seconds
    key = f"merge-wait-{uuid4().hex}"
    wait = WaitPhase(n, ctx, "merge-lock-wait", started)
    wait.open()

    def credit(close: bool = False):
        now = ctx.now()
        ctx.store.add_waited(
            n, key=key, start=started, end=now, close=close,
            waiting_until=None if close else (now + timedelta(seconds=60)).astimezone(UTC).isoformat(),
            last_progress_at=now.astimezone(UTC).isoformat(),
            merge_waited_seconds=previous_wait + max(
                0, (now - started).total_seconds(), ctx.monotonic() - started_mono,
                monotonic() - started_real,
            ),
        )
        return now

    completed = False
    try:
        yield credit
        completed = True
    finally:
        wait.close(credit(close=True), resume=completed)


def _wait_limit_reached(n: int, ctx: RunContext) -> bool:
    run = ctx.store.get_run(n)
    return run.merge_waited_seconds >= MERGE_WAIT_SECONDS


def _pause(n: int, ctx: RunContext) -> None:
    run = ctx.store.get_run(n)
    ctx.store.update_run(n, merge_wait_polls=run.merge_wait_polls + 1)
    ctx.sleep(min(POLL_SECONDS, max(0, MERGE_WAIT_SECONDS - run.merge_waited_seconds)))


def _report_wait_limit(n: int, ctx: RunContext, place: Seat) -> NoReturn:
    from foragent import orch_inbox
    from foragent.labels import BLOCKED, READY, RUNNING
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_checks import _append as append_log

    withdraw(ctx, place)
    run = ctx.store.get_run(n)
    detail = (f"Merge-turn wait limit ({MERGE_WAIT_SECONDS:g}s) reached after "
              f"{run.merge_waited_seconds:g}s ({run.merge_wait_polls} polls). "
              f"PR kept on {run.branch}; resume review of the existing PR when its merge turn can proceed.")
    orch_inbox.add_request(ctx.store, "runner", f"Issue #{n}: {detail}", {
        "kind": "merge-wait", "issue": n, "branch": run.branch,
        "run_started_at": run.started_at, "waited_seconds": run.merge_waited_seconds,
        "polls": run.merge_wait_polls,
    })
    append_log(ctx.store.log_path(n), detail)
    ctx.tracker.add_labels(n, [BLOCKED])
    ctx.tracker.remove_labels(n, [READY, RUNNING])
    raise ExecutorStopped("merge-wait", detail)


def await_turn(n: int, ctx: RunContext, place: Seat, head_sha: str) -> tuple[bool, str | None]:
    """Join and wait without re-verifying or polling CI; return (waited, stop).

    The whole merge-turn wait is credited as waited, preserving the work budget.
    Queue waits and retry pauses share a separate one-hour bound per run.
    """
    join(ctx, place, head_sha)
    if holds_turn(ctx.store, place):
        return False, None
    with _merge_wait(n, ctx) as credit:
        while not holds_turn(ctx.store, place):
            credit()
            if _wait_limit_reached(n, ctx):
                break
            _pause(n, ctx)
        else:
            return True, None
    _report_wait_limit(n, ctx, place)


def rejoin_after_failed_merge(n: int, ctx: RunContext, place: Seat, error: Exception) -> None:
    """A refused merge of an unchanged head: withdraw now and rejoin at the tail when ready again.

    The run is not ended; it takes a later turn (and appends another turn record).
    A short pause spaces retries out and is credited as waited, under the same
    separate bound as the run's merge-turn waits.
    """
    from foragent.run_checks import _append as append_log

    withdraw(ctx, place)
    append_log(ctx.store.log_path(n), f"Merge failed with the head unchanged; rejoining the merge queue: {error}")
    with _merge_wait(n, ctx) as credit:
        credit()
        if not _wait_limit_reached(n, ctx):
            _pause(n, ctx)
        credit()
        exhausted = _wait_limit_reached(n, ctx)
    if exhausted:
        _report_wait_limit(n, ctx, place)


def claim(ctx: RunContext, place: Seat, head_sha: str) -> int | None:
    """Take ``place.target``'s turn for a merge made outside a run, atomically.

    Under one state lock: return the issue of a live entry that holds the turn,
    or append this ready entry, so no run can join between the check and the join.
    Such an entry has no run record, so it is never live afterwards; the caller
    holds the merge lock for the whole merge, which keeps every later entry waiting.
    """
    with ctx.store._state_lock():
        entries = _waiting(_rows(ctx.store), place.target)
        for key in _turn_order(entries, orch_queue.get_global(ctx.store)):
            if _live(ctx.store, key):
                return key[0]
        _append(ctx.store, _row(ctx, place, "ready", head_sha=head_sha))
    return None

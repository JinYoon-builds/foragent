"""FIFO merge turn over the append-only ``<state>/merge-queue.jsonl`` (#712).

State ownership: each run appends only its own ``ready``/``withdrawn`` entries,
only the turn holder appends ``turn`` records, and the file only grows. The
turn itself is derived on every read, never stored. There is one FIFO per
target branch.

Reading the queue: a run's entry is keyed by issue and the run's started_at.
Its first ``ready`` entry fixes its position (a later head change keeps it). A
``withdrawn`` entry ends that position; a later ``ready`` joins at the tail. A
``turn`` record is history only: the holder keeps the turn until its run stops
running, so a successor never takes the turn while the merge is in flight. A
run that merged leaves no withdrawn entry. The head, which holds the turn, is
the first remaining entry whose run is still running. Entries of runs that
are not running are skipped, never deleted; the scheduler's crash recovery
decides that an owner is dead.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, timedelta
import json
from typing import TYPE_CHECKING
from uuid import uuid4

from foragent.run_waits import WaitPhase

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.state import StateStore

QUEUE = "merge-queue.jsonl"


@dataclass(frozen=True)
class Seat:
    """One run's identity in one target branch's FIFO."""

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
    for key in _waiting(_rows(store), place.target):
        if key == place.key:
            return True
        if _live(store, key):
            return False
    return True


def record_turn(ctx: RunContext, place: Seat, base_sha: str, result: str) -> None:
    """Only the turn holder writes this: one record per merge try."""
    with ctx.store._state_lock():
        _append(ctx.store, _row(ctx, place, "turn", members=[place.n], base_sha=base_sha, result=result))


@contextmanager
def _merge_wait(n: int, ctx: RunContext):
    """Credit one merge queue wait and keep its liveness visible to tick."""
    started = ctx.now()
    key = f"merge-wait-{uuid4().hex}"
    wait = WaitPhase(n, ctx, "merge-lock-wait", started)
    wait.open()

    def credit(close: bool = False):
        now = ctx.now()
        ctx.store.add_waited(
            n, key=key, start=started, end=now, close=close,
            waiting_until=None if close else (now + timedelta(seconds=60)).astimezone(UTC).isoformat(),
            last_progress_at=now.astimezone(UTC).isoformat(),
        )
        return now

    try:
        yield credit
    finally:
        wait.close(credit(close=True))


def await_turn(n: int, ctx: RunContext, place: Seat, head_sha: str) -> tuple[bool, str | None]:
    """Join and wait without re-verifying or polling CI; return (waited, stop).

    The whole merge-turn wait is credited as waited, preserving the work budget.
    """
    join(ctx, place, head_sha)
    if holds_turn(ctx.store, place):
        return False, None
    with _merge_wait(n, ctx) as credit:
        while not holds_turn(ctx.store, place):
            credit()
            ctx.sleep(30)
    return True, None


def rejoin_after_failed_merge(n: int, ctx: RunContext, place: Seat, error: Exception) -> None:
    """A refused merge of an unchanged head: withdraw now and rejoin at the tail when ready again.

    The run is not ended; it takes a later turn (and appends another turn record).
    A short pause spaces retries out and is credited as waited.
    """
    from foragent.run_checks import _append as append_log

    withdraw(ctx, place)
    append_log(ctx.store.log_path(n), f"Merge failed with the head unchanged; rejoining the merge queue: {error}")
    with _merge_wait(n, ctx) as credit:
        credit()
        ctx.sleep(30)


def claim(ctx: RunContext, place: Seat, head_sha: str) -> int | None:
    """Take ``place.target``'s turn for a merge made outside a run, atomically.

    Under one state lock: return the issue of a live entry that holds the turn,
    or append this ready entry, so no run can join between the check and the join.
    Such an entry has no run record, so it is never live afterwards; the caller
    holds the merge lock for the whole merge, which keeps every later entry waiting.
    """
    with ctx.store._state_lock():
        for key in _waiting(_rows(ctx.store), place.target):
            if _live(ctx.store, key):
                return key[0]
        _append(ctx.store, _row(ctx, place, "ready", head_sha=head_sha))
    return None

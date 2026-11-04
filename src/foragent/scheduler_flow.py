"""One tick's ready observations: why each ready issue did not start, kept only when that changes.

Branches of the tick name a cause and carry on; nothing here checks, reads GitHub, waits or
reorders. The tick lock makes this the only writer of ``ready`` records, and the last record of
each issue is the only memory. Observing never changes a tick's result or exception.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any

from foragent import resource_sample, scheduler
from foragent.state import StateStore

if TYPE_CHECKING:
    from foragent.scheduler import TickContext

RELEASED = "released"  # A waiting issue left the candidates without starting.
BLOCKED_BY = "blocked-by"
MERGE_WAIT_PHASES = frozenset({"merge-turn-wait", "merge-lock-wait"})


@dataclass(frozen=True)
class _Slots:
    limit: int
    occupied: int
    free: bool
    machine: bool  # False only where the project ceiling alone decided.


@dataclass
class _Tick:
    notes: dict[int, tuple[dict[str, Any], str | None]] = field(default_factory=dict)
    slots: _Slots | None = None
    complete: bool = False  # Every stored issue was considered on fresh facts, so silence means it left.


def _observing(call: Callable[..., None]) -> Callable[..., None]:
    @wraps(call)
    def guarded(ctx: TickContext, *args: Any, **kwargs: Any) -> None:
        if getattr(ctx, "_flow", None) is not None:
            try:
                call(ctx, *args, **kwargs)
            except Exception:
                return
    return guarded


@contextmanager
def recording(ctx: TickContext, acquired: bool) -> Iterator[None]:
    """Collect one acquired tick's causes and write the changed ones on every way out of it."""
    if not acquired:
        yield
        return
    ctx._flow = tick = _Tick()
    try:
        yield
    except BaseException:
        tick.complete = False
        raise
    finally:
        ctx._flow = None
        _flush(ctx.store, tick)


def _flush(store: StateStore, tick: _Tick) -> None:
    try:
        last = {row["issue"]: row for row in store.flow_records() if row["kind"] == "ready"}
        if tick.complete:
            for number, row in last.items():
                if (number not in tick.notes and row["data"].get("outcome") == "waiting"
                        and row["data"].get("reason") != RELEASED):
                    tick.notes[number] = ({"reason": RELEASED, "outcome": "waiting"}, None)
        for number, (data, run_started_at) in sorted(tick.notes.items()):
            previous = last.get(number)
            if previous is None or previous["data"] != data or previous["run_started_at"] != run_started_at:
                store.record_flow("ready", issue=number, run_started_at=run_started_at, **data)
    except Exception:
        return


@_observing
def note(ctx: TickContext, number: int, reason: str, **facts: Any) -> None:
    """This issue's own branch decided: it replaces any cause known earlier in the tick."""
    ctx._flow.notes[number] = ({"reason": reason, "outcome": "waiting", **facts}, None)


@_observing
def hold(ctx: TickContext, numbers: Iterable[int], reason: str, **facts: Any) -> None:
    """A global branch holds every candidate that has no cause of its own yet."""
    for number in numbers:
        ctx._flow.notes.setdefault(number, ({"reason": reason, "outcome": "waiting", **facts}, None))


@_observing
def started(ctx: TickContext, number: int, run_started_at: str | None) -> None:
    ctx._flow.notes[number] = ({"reason": None, "outcome": "started"}, run_started_at)


@_observing
def hold_stored(ctx: TickContext, reason: str) -> None:
    """The tick stops before its start loop: stored candidates wait on this branch."""
    hold(ctx, (row["number"] for row in ctx.store.local_issues()
               if row["state"] == "open" and {"approved", "ready"} <= set(row["labels"])
               and not set(row["labels"]) & {"blocked", "needs-human", "running"}), reason)


@_observing
def slots_measured(ctx: TickContext, limit: int, occupied: int, *, free: bool, machine: bool) -> None:
    """The figures `_has_slot` just decided on, the only ones a slot cause may carry."""
    ctx._flow.slots = _Slots(limit, occupied, free, machine)


@_observing
def hold_slots(ctx: TickContext, numbers: Iterable[int]) -> None:
    hold(ctx, numbers, "slots-full", **_slot_facts(ctx, free=False))


@_observing
def note_free_slot(ctx: TickContext, number: int) -> None:
    note(ctx, number, "free-slot", **_slot_facts(ctx, free=True))


def _slot_facts(ctx: TickContext, *, free: bool) -> dict[str, Any]:
    """The deciding measurement, never a new count; without a matching one the figures stay unknown."""
    slots = ctx._flow.slots
    if slots is None or slots.free != free:
        return {}
    roots = [ctx.store.root, *(resource_sample.machine_stores(ctx.store) if slots.machine else [])]
    return {"run_slots": {"limit": slots.limit, "occupied": slots.occupied,
                          "merge_waiters": _merge_waiters(roots, ctx.store)}}


def _merge_waiters(roots: list, store: StateStore) -> int | None:
    """Running runs waiting to merge; an unreadable store makes the count unknown, never a zero."""
    try:
        return sum(run.phase in MERGE_WAIT_PHASES
                   for root in roots for run in (store if root == store.root else StateStore(root)).active_runs())
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _flagged(row: dict[str, Any]) -> dict[str, Any] | None:
    """A blocked or needs-human label holds the issue until someone acts; a tick never retries it."""
    if waiting := sorted(set(row["labels"]) & {"blocked", "needs-human"}):
        return {"labels": waiting}
    return None


@_observing
def note_blockers(ctx: TickContext, number: int, dependencies: Iterable[Any]) -> None:
    """Open blockers decided, from the very read that decided: each known edge's state and repository."""
    note(ctx, number, BLOCKED_BY, blocked_by=[
        {"number": edge.number, "state": edge.state, "repository": edge.repository}
        for edge in sorted(dependencies, key=lambda edge: (edge.repository or "", edge.number))
    ])


@_observing
def note_ineligible(ctx: TickContext, number: int, labels: Iterable[str]) -> None:
    """Eligibility said no on these labels: they name the cause, not a later read of the issue."""
    if (facts := _flagged({"labels": labels})) is not None:
        note(ctx, number, "held-no-retry", **facts)
    else:
        note(ctx, number, "not-eligible")


@_observing
def note_checked(ctx: TickContext, number: int, otherwise: str) -> None:
    """A start check said no: the flag that check raised is the cause, or the blocker it named, else the check.

    Unlike slot figures this read must follow the decision: the check's own flag is what it reads.
    """
    row = next((row for row in ctx.store.local_issues() if row["number"] == number), {"labels": []})
    if (facts := _flagged(row)) is not None:
        note(ctx, number, "held-no-retry", **facts)
    elif ctx._flow.notes.get(number, ({},))[0].get("reason") != BLOCKED_BY:
        note(ctx, number, otherwise)


@_observing
def considered(ctx: TickContext, ready: dict[str, list[int]]) -> None:
    """All stored issues were considered: name the approved ones held before the candidates."""
    rows = ctx.store.local_issues()
    candidates = {number for numbers in ready.values() for number in numbers}
    for row in rows:
        if (row["number"] not in candidates and row["state"] == "open"
                and "approved" in row["labels"] and (facts := _flagged(row)) is not None):
            note(ctx, row["number"], "held-no-retry", **facts)
    ctx._flow.complete = not getattr(ctx, "_refresh_failed", False)

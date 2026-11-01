"""One tick's record of why each ready issue did not start, taken from its existing branches.

The tick's branches name a cause here and go on; this module adds no check, read
of GitHub, sleep or ordering. A global branch names the cause for every candidate
it held, so issues the loop never reached get that known cause, not a guess. Records
are written when the tick ends and only where an issue's reason or facts changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foragent.scheduler import TickContext
    from foragent.state import StateStore


@dataclass(frozen=True)
class SlotFacts:
    """What one slot answer measured: the bound limit, in the machine's or this project's scope."""

    limit: int
    occupied: int
    machine: bool


@dataclass
class _Tick:
    decided: dict[int, tuple[str | None, dict[str, Any]]] = field(default_factory=dict)
    ready: list[int] | None = None  # Set once this tick has judged its candidates.
    hold: tuple[str, dict[str, Any]] | None = None
    current: int | None = None  # The issue the tick is judging, so an exit during it has a known cause.
    failure: Exception | None = None


def _current(ctx: TickContext) -> _Tick | None:
    return getattr(ctx, "_ready_flow", None)


def considering(ctx: TickContext, number: int | None) -> None:
    if (tick := _current(ctx)) is not None:
        tick.current = number


def waits(ctx: TickContext, number: int, reason: str, **facts: Any) -> None:
    """This issue waits for ``reason``. Returns None, so a failed check can return it directly."""
    if (tick := _current(ctx)) is not None:
        tick.decided[number] = (reason, facts)


def hold(ctx: TickContext, reason: str, **facts: Any) -> None:
    """A global branch holds every candidate of this tick not already decided."""
    if (tick := _current(ctx)) is not None:
        tick.hold = (reason, facts)


def candidates(ctx: TickContext, ready: dict[str, list[int]]) -> None:
    if (tick := _current(ctx)) is not None:
        tick.ready = [number for numbers in ready.values() for number in numbers]


def hold_slots(ctx: TickContext) -> None:
    hold(ctx, "slots-full", run_slots=_run_slots(ctx))


def waits_at_width(ctx: TickContext, number: int) -> None:
    """A measured free machine slot that the plan's width keeps this candidate from using."""
    waits(ctx, number, "free-slot", run_slots=_run_slots(ctx))


def prerequisite(ctx: TickContext, number: int) -> None:
    """Name the failed prerequisite from what its check already read; no read happens here."""
    tick = _current(ctx)
    if tick is None:
        return
    try:
        edges = _open_edges(ctx, number)
    except Exception:  # noqa: BLE001
        edges = []
    if edges:
        waits(ctx, number, "blocked-by", blocked_by=edges)
    else:
        waits(ctx, number, "prerequisite")


def started(ctx: TickContext, number: int) -> None:
    if (tick := _current(ctx)) is not None:
        tick.decided[number] = (None, {})


def _open_edges(ctx: TickContext, number: int) -> list[dict[str, Any]]:
    row = next((row for row in ctx.store.local_issues() if row["number"] == number), {})
    open_pr = (row.get("reads") or {}).get("open_pr")
    if open_pr is not None and open_pr.get("value") is not None:
        return []  # The check stopped at the open PR, before any blocker it may remember.
    edges = ctx.store.flow_known_edges(number)
    if edges is None:
        from foragent.state_flow import normalized_edges

        read = (row.get("reads") or {}).get("blocked_by")  # What the failed check itself remembered.
        edges = normalized_edges(read["value"] if read is not None else row.get("blocked_by") or [])
    return [edge for edge in edges if edge.get("state") == "open"]


def _run_slots(ctx: TickContext) -> dict[str, Any] | None:
    facts = getattr(ctx, "_slot_facts", None)
    if facts is None:
        return None
    return {"limit": facts.limit, "occupied": facts.occupied,
            "merge_waiters": _merge_waiters(ctx.store, facts.machine)}


def _merge_waiters(store: StateStore, machine: bool) -> int | None:
    """Live merge seats in the bound limit's scope; unreadable is unknown, never zero."""
    from foragent import run_merge_turn
    from foragent.state import StateStore as Store

    def seats(one: Store) -> int:
        rows = run_merge_turn._rows(one)
        waiting = {key for target in {row.get("target") for row in rows} for key in run_merge_turn._waiting(rows, target)}
        return sum(run_merge_turn._live(one, key) for key in waiting)

    try:
        count = seats(store)
        if machine:
            for sibling in sorted(Path(store.root).parent.iterdir()):
                if sibling != store.root and (sibling / run_merge_turn.QUEUE).exists():
                    count += seats(Store(sibling))
        return count
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def recording(ctx: TickContext, acquired: bool = True) -> Iterator[None]:
    """Collect one acquired tick's reasons and write the changed ones however it ends."""
    if not acquired:
        yield
        return
    ctx._ready_flow, ctx._slot_facts = _Tick(), None
    try:
        yield
    except Exception as exc:
        ctx._ready_flow.failure = exc
        raise
    finally:
        tick, ctx._ready_flow = ctx._ready_flow, None
        try:
            _flush(ctx, tick)
        except Exception:  # noqa: BLE001
            pass  # Observation only: the tick's result and error stay its own.


def _flush(ctx: TickContext, tick: _Tick) -> None:
    from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
    from foragent.tracker import TrackerError

    store = ctx.store
    rows = {row["number"]: row for row in store.local_issues()}
    held = tick.ready if tick.ready is not None else [
        number for number, row in rows.items()
        if row["state"] == "open" and READY in row["labels"]
        and not set(row["labels"]) & {RUNNING, BLOCKED, NEEDS_HUMAN}]
    decided = dict(tick.decided)
    judged = rows.get(tick.current)
    if judged is not None and tick.current not in decided and judged["state"] == "open" and (
            set(judged["labels"]) & {APPROVED, READY}):
        # The tick ended while judging this issue: that exit is its cause, not its last one.
        if tick.failure is not None:
            kind = "read-failed" if isinstance(tick.failure, TrackerError) else "tick-failed"
            decided[tick.current] = (kind, {"error": type(tick.failure).__name__})
        elif tick.hold is not None:
            decided[tick.current] = tick.hold
    if tick.hold is not None:
        for number in held:
            decided.setdefault(number, tick.hold)
    changes = []
    for number, (reason, facts) in sorted(decided.items()):
        outcome = "waiting" if reason is not None else "started"
        run = store.get_run(number) if reason is None else None
        changes.append((number, run.started_at if run is not None else None,
                        {"reason": reason, "outcome": outcome, **facts}))
    if tick.ready is not None:
        # Every issue was judged this tick: one that waited and the tick neither held nor kept has left the candidates.
        for number, last in store._flow_last("ready").items():
            if number not in decided and number not in tick.ready and last["data"].get("outcome") == "waiting":
                changes.append((number, None, {"reason": "released", "outcome": "waiting"}))
    store._record_flow_changes("ready", changes)

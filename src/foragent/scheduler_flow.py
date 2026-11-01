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
    out: set[int] = field(default_factory=set)  # Judged this tick and found to be no candidate.
    failure: Exception | None = None


def _current(ctx: TickContext) -> _Tick | None:
    return getattr(ctx, "_ready_flow", None)


def considering(ctx: TickContext, number: int) -> None:
    if (tick := _current(ctx)) is not None:
        tick.current = number


def considered(ctx: TickContext, number: int, ready: dict[str, list[int]]) -> None:
    """Its judgment finished: without a reason or a place among the candidates, it is no candidate."""
    tick = _current(ctx)
    if tick is not None and number not in tick.decided and not any(number in numbers for numbers in ready.values()):
        tick.out.add(number)


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
    hold = tick.hold
    if hold is None and tick.failure is not None:  # A tick that failed held every candidate it had not decided.
        kind = "read-failed" if isinstance(tick.failure, TrackerError) else "tick-failed"
        hold = (kind, {"error": type(tick.failure).__name__})
    decided = dict(tick.decided)
    if hold is not None:
        held = tick.ready if tick.ready is not None else [
            number for number, row in rows.items()
            if row["state"] == "open" and READY in row["labels"]
            and not set(row["labels"]) & {RUNNING, BLOCKED, NEEDS_HUMAN}]
        judging = rows.get(tick.current)
        if judging is not None and judging["state"] == "open" and set(judging["labels"]) & {APPROVED, READY}:
            held = [*held, tick.current]  # The tick ended while judging it, ready or not.
        for number in held:
            if number not in tick.out:
                decided.setdefault(number, hold)
    started = {number for number, (reason, _) in decided.items() if reason is None}
    waited = {number for number, last in store._flow_last("ready").items() if last["data"].get("outcome") == "waiting"}
    # A candidate left when its row is gone (a 404 removes it during the tick) or its judgment passed it by.
    left = {number for number in waited - started
            if number not in rows or (number in tick.out and number not in decided)}
    changes = [(number, None, {"reason": "released", "outcome": "waiting"}) for number in sorted(left)]
    for number, (reason, facts) in sorted(decided.items()):
        if reason is not None and number not in rows:
            continue  # Nothing waits for an issue that is gone. A start already happened, whatever came after it.
        run = store.get_run(number) if reason is None else None
        changes.append((number, run.started_at if run is not None else None,
                        {"reason": reason, "outcome": "waiting" if reason is not None else "started", **facts}))
    store._record_flow_changes("ready", changes)

"""Money a session spent, as its executor reported it (#786)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.state import StateStore


def record_cost(store: StateStore, name: str, cost: float | None) -> float | None:
    """Record a money session's reported cost against ``name``; nothing when none was reported."""
    if cost is not None:
        store.record_spend(name, cost)
    return cost

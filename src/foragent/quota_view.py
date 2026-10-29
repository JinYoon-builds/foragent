"""One read of the quota facts a decision uses (#186): usage, executor pauses and spend.

A decision that asks several questions (who implements, who reviews, does the start wait,
does a launch hold, does it spend credits) answers all of them from one ``QuotaView``.
Another run may write a pause, a usage reading or a charge at any moment; reading the
facts again between two questions could then give two answers that disagree.

The view stands in for the ``StateStore`` in the quota functions: the quota reads come
from the snapshot, and everything else (records, locks, writes) goes to the store.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foragent.state import StateStore


class QuotaView:
    def __init__(self, store: StateStore, now: datetime):
        from foragent.scheduler_quota import _usage

        self.store, self.now = store, now
        self._usage = _usage(store)
        self._pauses = store.executor_pauses(now)
        self._spent: dict[str, float] = {}

    def usage(self) -> dict:
        return self._usage

    def executor_pause_until(self, executor: str, now: datetime | None = None) -> datetime | None:
        return self._pauses.get(executor)

    def executor_paused(self, executor: str, now: datetime | None = None) -> bool:
        return executor in self._pauses

    def spend_today(self, executor: str, now: datetime | None = None) -> float:
        if executor not in self._spent:  # Read once per executor, then kept for this decision.
            self._spent[executor] = self.store.spend_today(executor, self.now)
        return self._spent[executor]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)


def view(store: StateStore | QuotaView, now: datetime) -> QuotaView:
    """``store`` as a quota view; an existing view is reused so nested calls share its snapshot."""
    return store if isinstance(store, QuotaView) else QuotaView(store, now)

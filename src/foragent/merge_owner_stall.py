"""Raise a merge stall once: ready merge-queue members and no merge on main for 60 minutes (#2843).

One stall is named by the time of the last merge it follows. The inbox request carries that
time (``merge_stall_since``), so the inbox is the only proof a stall was raised: a failed
write leaves nothing that suppresses the next tick's retry, an answered request still
counts, and a new merge starts a new stall. Callers run under the tick lock, one at a time.
"""

from datetime import datetime, timedelta
from typing import Any

from foragent import merge_owner, orch_inbox
from foragent.state import StateStore

STALL = timedelta(minutes=60)
REASONS = 3  # Latest train retire reasons named in the request.


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def last_merge(store: StateStore, now: datetime) -> datetime | None:
    """The latest recorded merge moment (worker or merge owner) at or before ``now``."""
    times = [at for value in store.merge_times().values() if (at := _time(value)) is not None and at <= now]
    return max(times, default=None)


def ready_members(store: StateStore) -> list[int]:
    """Issue numbers waiting in the merge owner's queue."""
    return sorted({number for number, _ in merge_owner.admissions(store)})


def retire_reasons(store: StateStore) -> list[str]:
    """The latest train retire reasons, newest first."""
    reasons = [str(row["data"]["reason"]) for row in store.flow_records()
               if row.get("kind") == "merge-batch" and isinstance(row.get("data"), dict)
               and row["data"].get("reason")]
    return list(reversed(reasons))[:REASONS]


def request_text(members: list[int], since: datetime, now: datetime, reasons: list[str]) -> str:
    minutes = int((now - since).total_seconds() // 60)
    lines = [
        f"Merge stall: no merge landed on main for {minutes} minutes (last merge {since.isoformat()}) "
        f"while merge-queue members are ready: {', '.join(f'#{n}' for n in members)}.",
        "Latest train retire reasons: " + ("; ".join(reasons) if reasons else "none recorded") + ".",
        "Find why the trains retire and decide what unblocks the queue.",
    ]
    return "\n".join(lines)


def post_if_stalled(store: StateStore) -> dict[str, Any] | None:
    """Put one request in the orchestrator inbox per stall; return it when posted."""
    now = store._now()
    since = last_merge(store, now)
    if since is None or now - since < STALL:
        return None
    members = ready_members(store)
    if not members:
        return None
    key = since.isoformat()
    if any(row["from"] == "gate" and row.get("merge_stall_since") == key for row in orch_inbox.requests(store)):
        return None
    reasons = retire_reasons(store)
    return orch_inbox.add_request(store, "gate", request_text(members, since, now, reasons),
                                  {"merge_stall_since": key, "members": members, "retire_reasons": reasons})

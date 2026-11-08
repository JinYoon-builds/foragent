"""Which queued work can start now, counted positively from local records (#2824).

One reading for every consumer: the published startable and no-approval counts (now.json)
and the finish estimate both come from ``startable``, so a hold the gate recorded for an
issue leaves both figures at once.
"""

from typing import Any

from foragent.state import StateStore


class Queue(tuple):
    """(startable, no_approval, held) issue numbers."""

    can_start = property(lambda self: self[0])
    no_approval = property(lambda self: self[1])
    held = property(lambda self: self[2])


def startable(store: StateStore, status_facts: dict[str, Any],
              rows: list[dict[str, Any]] | None = None) -> Queue | None:
    """(startable, ready-without-approval, held) numbers, or None when a record is unreadable.

    Held: open approved work that a record holds right now and no live run works on: parked,
    a person or a block (not an open PR, which waits to merge), or a hold reason in the
    gate's latest ``ready`` record for it.

    Startable: open, approved and ready, in the queue (no person, block or worker holds it, no
    PR waits), not parked, no open dependency, and the gate's latest ``ready`` record for it
    names no hold reason. It is never the queue minus named holds: a hold the view cannot
    name must not make an issue look startable. Parking takes the approval label away, so a
    parked issue is parked, not unapproved; held issues are not counted as unapproved either.
    """
    from foragent import status, watch_lane_facts

    try:
        rows = store.local_issues() if rows is None else rows
        latest = {row["issue"]: row for row in store.flow_records() if row.get("kind") == "ready"}
        events = watch_lane_facts.journal(store)
    except Exception:  # noqa: BLE001 - an unreadable record makes the counts unknown, never zero.
        return None
    if events is None:
        return None
    parked = {number for number, fact in watch_lane_facts.park_facts(events).items() if fact["parked"]}
    active = {run.get("number") for run in status_facts.get("active_runs") or () if isinstance(run, dict)}
    states = {row["number"]: row["state"] for row in rows}
    can_start: set[int] = set()
    no_approval: set[int] = set()
    held: set[int] = set()
    for row in rows:
        labels = set(row.get("labels") or ())
        number = row["number"]
        if row.get("state") != "open":
            continue
        group = status.local_group(row)
        state = status.issue_state(labels, number in active, group)
        record = latest.get(number)
        reason = isinstance(record, dict) and (record.get("data") or {}).get("reason")
        if status.APPROVED in labels and number not in active and (
                number in parked or reason or (state in {"waiting-human", "blocked"} and group != "pr-open")):
            held.add(number)
        if status.READY not in labels or number in parked:
            continue
        if state != "queued":
            continue  # A person, a block, a worker or an open PR holds it.
        if status.APPROVED not in labels:
            no_approval.add(number)
            continue
        waits = [dep for dep in row.get("blocked_by") or ()
                 if states.get(dep.get("number"), dep.get("state")) != "closed"]
        if not waits and not reason:
            can_start.add(number)
    return Queue((can_start, no_approval, held))

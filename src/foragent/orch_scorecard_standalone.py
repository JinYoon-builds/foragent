"""Standalone latency for `fa orch scorecard`: size s issues approved into an otherwise empty queue.

A standalone sample is a merged size s issue whose latest approval before its merge came while
no other executable issue of this repository was queued or running. It measures approval to the
recorded merge and sits in the bucket of its merge.

The approval time, the size and whether the queue was empty all come only from the fact the
gate's approval step wrote when it saved that approval (``scheduler_approval_queue``, one writer,
append only). Nothing is read from the overwritable approval snapshot, the label cache, later
observations or run records, so later changes cannot move a past sample. A merged issue with no
fact before its merge (approved before the writer existed, or its fact rotated out of the
retained flow records) counts as missing evidence: its queue and size at approval are unknown.
Evidence limit: if writing a later re-approval's fact failed, the earlier fact is the one read.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from foragent import orch_scorecard_flow as flow
from foragent.orch_facts_resources import _time
from foragent.orch_scorecard_records import _executions
from foragent.scheduler_approval_queue import KIND
from foragent.state import StateStore


def _facts(store: StateStore) -> dict[int, dict[datetime, dict[str, Any]]]:
    """Each issue's retained approval-queue facts by approval time; a repeat of one approval keeps
    the later record, which describes the same moment."""
    found: dict[int, dict[datetime, dict[str, Any]]] = {}
    for record in store.flow_records():
        data = record.get("data") or {}
        if (record.get("kind") == KIND and type(record.get("issue")) is int
                and (at := _time(data.get("approved_at"))) is not None and isinstance(data.get("others"), list)):
            found.setdefault(record["issue"], {})[at] = data
    return found


def _bucket(moment: datetime, now: datetime, days: int) -> int | None:
    if moment > now:
        return None
    index = (now - moment) // flow.DAY
    return index if index < days else None


def _summary(minutes: list[float], missing: int) -> dict[str, Any]:
    return {"samples": len(minutes), "median_minutes": median(minutes) if minutes else None,
            "missing_evidence": missing}


def windows(store: StateStore, now: datetime, days: int, lanes: dict | None = None) -> tuple[dict, dict]:
    """(today, previous): samples, median minutes and missing-evidence count.

    The previous window pools every earlier bucket's samples, so its median is over issues,
    not a median of daily medians. ``lanes`` is accepted for the card's call shape; no lane
    rule applies here."""
    facts = _facts(store)
    minutes: list[list[float]] = [[] for _ in range(days)]
    missing = [0] * days
    merges: dict[int, datetime] = {}
    for row in _executions(store, now):  # Each issue's latest merge; a merged issue counts once.
        if row["merged"] and row["merged_at"] is not None:
            merges[row["number"]] = max(row["merged_at"], merges.get(row["number"], row["merged_at"]))
    for number, merged in merges.items():
        if (bucket := _bucket(merged, now, days)) is None:
            continue
        # The approval the merge followed is the latest fact written at or before the merge.
        approved = max((at for at in facts.get(number, {}) if at <= merged), default=None)
        if approved is None:
            missing[bucket] += 1  # No approval-time fact: queue and size at approval are unknown.
            continue
        fact = facts[number][approved]
        if fact.get("size") == "s" and not fact["others"]:
            minutes[bucket].append((merged - approved).total_seconds() / 60)
    today = _summary(minutes[0], missing[0])
    previous = _summary([value for day in minutes[1:] for value in day], sum(missing[1:]))
    return today, previous

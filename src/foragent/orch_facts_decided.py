"""Decided issues waiting for tests, and decision-to-approval time, from local records only.

An issue waits for tests while it is open and a Done when command is still the
placeholder the order writes before tests exist. Decision-to-approval time runs from
the issue's creation on GitHub (recorded on the issue row when the gate read it) to
the approval the gate recorded. Missing evidence is unknown (None), never zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from statistics import median
from typing import Any

from foragent.issue import parse_body
from foragent.state import StateStore
from foragent.state_observations import stamp

PLACEHOLDER = re.compile(r"false\s+#\s*placeholder:")


def is_placeholder(command: str) -> bool:
    return PLACEHOLDER.match(command.strip()) is not None


def tests_waiting(store: StateStore) -> dict[str, Any]:
    """Open issues whose Done when is still the placeholder; unknown before any complete listing."""
    record = store._issue_record()
    if stamp(record.get("complete_at")) is None:
        return {"count": None, "issues": None}
    numbers = sorted(
        row["number"] for row in record["issues"].values()
        if row.get("observed_at") is not None and not row.get("is_pull_request") and row.get("state") == "open"
        and any(is_placeholder(command) for command in parse_body(row.get("body") or "").done_when_commands))
    return {"count": len(numbers), "issues": numbers}


def approvals(store: StateStore) -> list[tuple[datetime, float | None]]:
    """(approved at, minutes since GitHub creation or None) for each recorded approval."""
    rows = store._issue_record()["issues"]
    found = []
    for path in sorted((store.root / "approvals").glob("*.json")):
        if not path.stem.isdigit() or (approval := store.load_approval(int(path.stem))) is None:
            continue
        approved = stamp(approval.approved_at)
        if approved is None:
            continue
        created = stamp((rows.get(path.stem) or {}).get("created_at"))
        minutes = (approved - created).total_seconds() / 60 if created is not None and created <= approved else None
        found.append((approved, minutes))
    return found


def summary(items: list[tuple[datetime, float | None]]) -> dict[str, Any]:
    known = [minutes for _, minutes in items if minutes is not None]
    return {"approved": len(items), "known": len(known), "unknown": len(items) - len(known),
            "median_minutes": median(known) if known else None}


def decision_to_approval(store: StateStore, now: datetime, window: timedelta) -> dict[str, Any]:
    """Approvals recorded in ``(now - window, now]`` and the median minutes of the known ones."""
    return summary([item for item in approvals(store) if now - window < item[0] <= now])


def collect(store: StateStore, now: datetime, window: timedelta) -> dict[str, Any]:
    return {"tests_waiting": tests_waiting(store), "decision_to_approval": decision_to_approval(store, now, window)}

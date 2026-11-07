"""Five-stage lead time of merged order children, from local preparation, issue and decision records.

An order child's body carries a ``foragent:plan-child`` marker that names its order's
preparation record. Its stages are dated as follows: "ordered" is the earliest attempt
start, "prepared" the finish of the last reviewed reviewer attempt, "published" the issue
row's ``created_at``, "started" the run start and "merged" the merge record. A decision
linked by the body's decisions marker dates "decided" from docs/decisions.md in the working
tree (by day); without one the lead time starts at "ordered" and is marked so.
An issue sits in the 24-hour bucket of its merge, the same buckets as orch_scorecard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from statistics import median
from typing import Any

from foragent import decisions, order_prepare
from foragent.orch_facts_resources import _time
from foragent.state import StateStore

DAY = timedelta(days=1)
STAGES = ("decided->ordered", "ordered->prepared", "prepared->published", "published->started",
          "started->merged")
CHILD = re.compile(r"<!-- foragent:plan-child id=(\S+) key=(\S+) -->")
DATE = r"\d{4}-\d{2}-\d{2}"


def _median(values) -> float | None:
    known = [value for value in values if value is not None]
    return median(known) if known else None


def _date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def decision_dates(root: Path | None) -> dict[str, datetime]:
    """Each decision's date: its heading's own date, else its ``## <date>`` section. No Git call."""
    if root is None:
        return {}
    try:
        ledger = (Path(root) / decisions.LEDGER).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    found: dict[str, datetime] = {}
    section = None
    for line in ledger.splitlines():
        if line.startswith("## "):
            date = re.match(rf"## ({DATE})(?:\s|$)", line)
            section = _date(date[1]) if date else None
        if heading := decisions.HEADING.match(line):
            date = re.search(rf"\(({DATE})(?:\D|$)", line)
            moment = (_date(date[1]) if date else None) or section
            if moment is not None:
                found.setdefault(heading[1], moment)
    return found


def _attempts(store: StateStore, body: str) -> list[dict[str, Any]] | None:
    child = CHILD.search(body)
    if child is None:
        return None
    try:
        record = json.loads(order_prepare._record_path(store, {"id": child[1]}).read_text(encoding="utf-8"))
        attempts = record["issues"][child[2]]["attempts"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return attempts if isinstance(attempts, list) else None


def entry(store: StateStore, issue: dict[str, Any], execution: dict[str, Any],
          dates: dict[str, datetime]) -> dict[str, Any] | None:
    """One merged order child's stages in minutes; None when it has no order record."""
    body = issue.get("body") or ""
    attempts = _attempts(store, body)
    if attempts is None:
        return None
    attempts = [attempt for attempt in attempts if isinstance(attempt, dict)]
    ordered = min((moment for attempt in attempts if (moment := _time(attempt.get("started_at"))) is not None),
                  default=None)
    if ordered is None:
        return None
    reviewed = [attempt for attempt in attempts
                if attempt.get("role") == "reviewer" and attempt.get("status") == "reviewed"]
    prepared = _time(reviewed[-1].get("finished_at")) if reviewed else None
    decided = min((dates[key] for key in decisions.explicit(body) or [] if key in dates), default=None)
    moments = (decided, ordered, prepared, _time(issue.get("created_at")), execution["started"],
               execution["merged_at"])
    stages = {stage: (end - begin).total_seconds() / 60 if begin is not None and end is not None else None
              for stage, begin, end in zip(STAGES, moments, moments[1:])}
    return {"issue": issue["number"], "start": "decided" if decided is not None else "ordered",
            "stages": stages, "minutes": (execution["merged_at"] - (decided or ordered)).total_seconds() / 60}


def windows(store: StateStore, now: datetime, days: int, executions: list[dict[str, Any]],
            root: Path | None) -> list[dict[str, Any]]:
    """Per bucket, today first: ``{"medians": {stage: minutes}, "issues": [...]}``."""
    dates = decision_dates(root)
    issues = {issue["number"]: issue for issue in store.local_issues()}
    out: list[dict[str, Any]] = [{"medians": dict.fromkeys(STAGES), "issues": []} for _ in range(days)]
    merges = {(row["number"], row["merged_at"]): row for row in executions
              if row["merged"] and row["merged_at"] is not None and row["merged_at"] <= now}
    for (number, merged), execution in sorted(merges.items(), key=lambda item: (item[0][1], item[0][0])):
        day = int((now - merged) // DAY)
        if day < days and number in issues and (value := entry(store, issues[number], execution, dates)):
            out[day]["issues"].append(value)
    for day in out:
        day["medians"] = {stage: _median(value["stages"][stage] for value in day["issues"]) for stage in STAGES}
    return out


def previous(days: list[dict[str, Any]]) -> dict[str, Any]:
    """The earlier window: each stage's median of the daily medians, and every issue."""
    return {"medians": {stage: _median(day["medians"][stage] for day in days) for stage in STAGES},
            "issues": [value for day in days for value in day["issues"]]}


def worse_names(today: dict[str, Any], before: dict[str, Any]) -> list[str]:
    """``lead_time.<stage>`` for the one stage whose median grew most; none when nothing grew."""
    growth = {stage: today["medians"][stage] - before["medians"][stage] for stage in STAGES
              if today["medians"][stage] is not None and before["medians"][stage] is not None
              and today["medians"][stage] > before["medians"][stage]}
    return [f"lead_time.{max(growth, key=growth.get)}"] if growth else []

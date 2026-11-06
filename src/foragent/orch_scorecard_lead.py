"""Five-stage order-child lead time from local preparation, issue and decision records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re

from foragent import decisions, order_prepare
from foragent.orch_facts_resources import _time
from foragent.orch_scorecard_four import mid

DAY = timedelta(days=1)
STAGES = ("decided->ordered", "ordered->prepared", "prepared->published", "published->started", "started->merged")
CHILD = re.compile(r"<!-- foragent:plan-child id=(\S+) key=(\S+) -->")
DATE = r"\d{4}-\d{2}-\d{2}"


def _date(value):
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def decision_dates(root: Path | None) -> dict[str, datetime]:
    """Read the working tree, including an untracked ledger; never invoke Git."""
    if root is None:
        return {}
    try:
        ledger = (root / decisions.LEDGER).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    found, section = {}, None
    for line in ledger.splitlines():
        if line.startswith("## "):
            date = re.match(rf"## ({DATE})(?:\s|$)", line)
            section = _date(date[1]) if date else None
        if heading := decisions.HEADING.match(line):
            date = re.search(rf"\(({DATE})(?:\D|$)", line)
            at = (_date(date[1]) if date else None) or section
            if at is not None:
                found[heading[1]] = at
    return found


def entry(store, issue, execution, dates):
    body = issue.get("body") or ""
    child = CHILD.search(body)
    if child is None:
        return None
    try:
        record = json.loads(order_prepare._record_path(store, {"id": child[1]}).read_text(encoding="utf-8"))
        attempts = record["issues"][child[2]]["attempts"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ordered = min((at for attempt in attempts if (at := _time(attempt.get("started_at"))) is not None), default=None)
    if ordered is None:
        return None
    reviewed = [attempt for attempt in attempts if attempt.get("role") == "reviewer"
                and attempt.get("status") == "reviewed"]
    prepared = _time(reviewed[-1].get("finished_at")) if reviewed else None
    decided = min((dates[key] for key in decisions.explicit(body) or [] if key in dates), default=None)
    moments = (decided, ordered, prepared, _time(issue.get("created_at")), execution["started"], execution["merged_at"])
    stages = {stage: (b - a).total_seconds() / 60 if a is not None and b is not None else None
              for stage, a, b in zip(STAGES, moments, moments[1:])}
    return {"issue": issue["number"], "start": "decided" if decided is not None else "ordered", "stages": stages,
            "minutes": (execution["merged_at"] - (decided or ordered)).total_seconds() / 60}


def windows(store, now, days, executions, root=None):
    dates = decision_dates(root)
    issues = {issue["number"]: issue for issue in store.local_issues()}
    out = [{"medians": dict.fromkeys(STAGES), "issues": []} for _ in range(days)]
    merges = {(row["number"], row["merged_at"]): row for row in executions
              if row["merged"] and row["merged_at"] is not None and row["merged_at"] <= now}
    for (number, merged), execution in sorted(merges.items(), key=lambda item: (item[0][1], item[0][0])):
        day = int((now - merged) // DAY)
        if day < days and number in issues and (value := entry(store, issues[number], execution, dates)) is not None:
            out[day]["issues"].append(value)
    for day in out:
        day["medians"] = {stage: mid(value["stages"][stage] for value in day["issues"]) for stage in STAGES}
    return out


def previous(days):
    return {"medians": {stage: mid(day["medians"][stage] for day in days) for stage in STAGES},
            "issues": [entry for day in days for entry in day["issues"]]}


def worse_names(today, before):
    increases = {stage: today["medians"][stage] - before["medians"][stage] for stage in STAGES
                 if today["medians"][stage] is not None and before["medians"][stage] is not None
                 and today["medians"][stage] > before["medians"][stage]}
    return [f"lead_time.{max(increases, key=increases.get)}"] if increases else []

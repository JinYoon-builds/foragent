"""D77 main-test quality for `fa orch scorecard`: failures of the base branch's checks after merges.

`fa orch scorecard --record-main-tests FILE` appends one supplied observation export to
``<state>/main-test-observations.jsonl``; the scorecard later reads only that file, so the
measure needs no GitHub call. An export is::

    {"since": ISO, "until": ISO, "complete": bool,
     "observations": [{"change", "check", "result", "at", "branch"}, ...]}

``since``..``until`` is the time the export watched; ``complete`` says whether it saw every
check result in it. A failure is one (change, check, result) seen failing on the base branch:
seeing the same result again, in one export or a later one, never counts another failure.
Observation never names a culprit merge.

A day is ``complete`` when complete exports cover all of it, ``partial`` when any export
touches it, else ``unavailable``. A count is known for a complete day; a partial day shows
the failures it saw (or ``None`` when it saw none), never a clean zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from statistics import median
from typing import Any

from foragent.orch_facts_resources import _jsonl, _time
from foragent.state import StateStore

FILE = "main-test-observations.jsonl"
DAY = timedelta(hours=24)
# GitHub check conclusions and common CI words that mean the tests did not pass.
FAILED = {"failure", "failed", "error", "timed_out", "startup_failure"}
FIELDS = ("change", "check", "result", "at")


class ExportError(ValueError):
    """The export file cannot be read as an observation export; nothing was recorded."""


def _parse(path: Path, base_branch: str) -> dict[str, Any]:
    try:
        export = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ExportError(f"cannot read {path}: {error}") from error
    if not isinstance(export, dict):
        raise ExportError(f"{path} must hold one JSON object")
    since, until = _time(export.get("since")), _time(export.get("until"))
    if since is None or until is None or since > until:
        raise ExportError(f"{path} needs ISO 'since' and 'until' times with since <= until")
    if not isinstance(export.get("complete"), bool):
        raise ExportError(f"{path} needs 'complete': true or false")
    rows = export.get("observations")
    if not isinstance(rows, list):
        raise ExportError(f"{path} needs an 'observations' list")
    kept = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not all(isinstance(row.get(key), str) and row[key] for key in FIELDS):
            raise ExportError(f"observation {index} needs text fields {', '.join(FIELDS)}")
        if _time(row["at"]) is None:
            raise ExportError(f"observation {index} has no ISO time in 'at'")
        if row.get("branch", base_branch) == base_branch:
            kept.append({key: row[key] for key in FIELDS} | {"branch": base_branch})
    return {"since": since.isoformat(), "until": until.isoformat(), "complete": export["complete"],
            "observations": kept}


def record(store: StateStore, path: Path, base_branch: str = "main") -> int:
    """Append one export (base-branch rows only); the number of observations not seen before."""
    entry = _parse(path, base_branch)
    seen = {_key(row) for row, _ in _observations(store)}
    fresh = {_key(row) for row in entry["observations"]} - seen
    entry["recorded_at"] = store._now().isoformat()
    with store._state_lock():
        store.root.mkdir(parents=True, exist_ok=True)
        with (store.root / FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return len(fresh)


def _key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["change"], row["check"], row["result"]


def _observations(store: StateStore) -> list[tuple[dict[str, Any], datetime]]:
    """Each distinct (change, check, result) once, at its earliest observed time."""
    first: dict[tuple[str, str, str], tuple[dict[str, Any], datetime]] = {}
    for entry in _jsonl(store.root / FILE):
        for row in entry.get("observations") or []:
            at = _time(row.get("at")) if isinstance(row, dict) else None
            if at is None or not all(isinstance(row.get(key), str) for key in FIELDS):
                continue
            key = _key(row)
            if key not in first or at < first[key][1]:
                first[key] = ({name: row[name] for name in FIELDS}, at)
    return list(first.values())


def _intervals(store: StateStore) -> list[tuple[datetime, datetime, bool]]:
    rows = []
    for entry in _jsonl(store.root / FILE):
        since, until = _time(entry.get("since")), _time(entry.get("until"))
        if since is not None and until is not None and since <= until:
            rows.append((since, until, entry.get("complete") is True))
    return rows


def _covered(start: datetime, end: datetime, spans: list[tuple[datetime, datetime]]) -> bool:
    """Whether the union of ``spans`` covers all of ``start..end``."""
    reach = start
    for since, until in sorted(spans):
        if since > reach:
            return False
        reach = max(reach, until)
        if reach >= end:
            return True
    return reach >= end


def _coverage(start: datetime, end: datetime, intervals: list[tuple[datetime, datetime, bool]]) -> str:
    if _covered(start, end, [(since, until) for since, until, complete in intervals if complete]):
        return "complete"
    if any(since < end and until > start for since, until, _ in intervals):
        return "partial"
    return "unavailable"


def daily(store: StateStore, now: datetime, days: int) -> list[dict[str, Any]]:
    """Per-day {count, coverage, failures}, today first; a day is ``(now-(k+1)d, now-kd]``."""
    intervals, failures = _intervals(store), [[] for _ in range(days)]
    for row, at in _observations(store):
        if row["result"].lower() in FAILED and at <= now and (index := (now - at) // DAY) < days:
            failures[index].append((at, row))
    result = []
    for index in range(days):
        end = now - index * DAY
        coverage = _coverage(end - DAY, end, intervals)
        rows = [row for _, row in sorted(failures[index], key=lambda item: (item[0], item[1]["check"]))]
        if coverage == "unavailable" and rows:
            coverage = "partial"  # A failure seen here is a fact even when its export's span missed it.
        count = len(rows) if coverage == "complete" or rows else None
        result.append({"count": count, "coverage": coverage, "failures": rows})
    return result


def previous(days: list[dict[str, Any]]) -> dict[str, Any]:
    """The daily median over the earlier days whose count is complete; coverage says how many were."""
    known = [day["count"] for day in days if day["coverage"] == "complete"]
    if days and len(known) == len(days):
        coverage = "complete"
    elif any(day["coverage"] != "unavailable" for day in days):
        coverage = "partial"
    else:
        coverage = "unavailable"
    return {"count": median(known) if known else None, "coverage": coverage,
            "failures": [row for day in days for row in day["failures"]]}


def windows(store: StateStore, now: datetime, days: int) -> tuple[dict[str, Any], dict[str, Any]]:
    per_day = daily(store, now, days)
    return per_day[0], previous(per_day[1:])

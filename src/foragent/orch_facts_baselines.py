"""Baseline build history and its trend against the capture limit, for `fa orch facts`.

Reads the machine-wide baseline cache directly; nothing is written, started or signalled.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent.i18n import text
from foragent.run_baseline_limits import capture_limit, latest_success, near_limit, read_entries


def collect(cache_root: Path, repo_root: Path, repo: str | None, now: datetime, window: timedelta) -> dict[str, Any]:
    entries = read_entries(cache_root, Path(repo_root), repo, now)
    # The limit as the runner computes it: all history, without a run's remaining cap.
    latest = latest_success(entries)
    limit = capture_limit(None if latest is None else latest.duration_seconds)
    rows, measured = [], []
    for entry in entries:
        if entry.created_at < now - window:
            continue
        count = len(entry.saved.get("node_ids") or ())
        # A successful zero-test capture measured nothing.
        duration = entry.duration_seconds if entry.timed_out or count else None
        rows.append({
            "key": entry.key, "base_sha": entry.saved.get("base_sha"), "created_at": entry.saved["created_at"],
            "test_count": count, "duration_seconds": duration, "duration_estimated": entry.estimated and duration is not None,
            "succeeded": not entry.timed_out, "timed_out": entry.timed_out,
        })
        if not entry.timed_out and duration is not None:
            measured.append(duration)
    newest = measured[0] if measured else None
    return {
        "baseline_builds": rows,
        "baseline_trend": {
            "latest_duration_seconds": newest, "capture_limit_seconds": limit,
            "ratio": None if newest is None else newest / limit, "near_limit": near_limit(measured, limit),
        },
    }


def human_line(facts: dict[str, Any], language: str) -> str:
    trend = facts["baseline_trend"]
    if trend["latest_duration_seconds"] is None:
        return text("facts_baseline_none", language, limit=trend["capture_limit_seconds"])
    line = text("facts_baseline", language, duration=f"{trend['latest_duration_seconds']:.0f}",
                share=f"{trend['ratio'] * 100:.0f}", limit=trend["capture_limit_seconds"])
    return line + (text("facts_baseline_near", language) if trend["near_limit"] else "")

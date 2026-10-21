"""Lane visibility derived from issue state and persisted runner activity."""

from datetime import datetime
from typing import Any

from foragent.config import Config
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane, lane_display, lane_order
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker


def _age(timestamp: str | None, now: datetime) -> float | None:
    return max(0, (now - datetime.fromisoformat(timestamp)).total_seconds()) if timestamp else None


def collect_lanes(
    issues: list[Issue], groups: dict[str, list[dict[str, Any]]], tracker: Tracker,
    store: StateStore, config: Config, now: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Keep queue entries while choosing the most urgent visible item per lane."""
    lanes: dict[str, dict[str, Any]] = {}
    summary = dict(working=0, blocked=0, waiting_human=0, queued=0)
    priority = {"working": 0, "waiting-human": 1, "blocked": 2, "queued": 3, "idle": 4}
    grouped = {item["number"]: (group, item) for group, items in groups.items() for item in items}

    def lane_row(slug: str) -> dict[str, Any]:
        return lanes.setdefault(slug, {
            "slug": slug, "lane_display": lane_display(config, slug, config.language),
            "state": "idle", "issue": None, "title": None, "phase": None,
            "elapsed_seconds": None, "last_activity_seconds": None, "waiting_for": [],
            RUNNING: [], READY: [], "queued": [],
        })

    for slug in {*config.lanes, "main"}:
        lane_row(slug)
    for issue in issues:
        run = store.get_run(issue.number)
        active = run is not None and run.status == "running"
        slug = run.lane if active else issue_lane(issue.labels, config.lanes)
        row = lane_row(slug)
        group, item = grouped[issue.number]
        if group in {RUNNING, READY}:
            row[group].append(item)
        if NEEDS_HUMAN in issue.labels:
            state = "waiting-human"
        elif BLOCKED in issue.labels:
            state = "blocked"
        elif active or RUNNING in issue.labels:
            state = "working"
        elif group == "pr-open":
            state = "waiting-human"
        elif APPROVED in issue.labels or READY in issue.labels:
            state = "queued"
        else:
            continue
        summary[state.replace("-", "_")] += 1
        if state == "queued":
            row["queued"].append(item)
        if priority[state] >= priority[row["state"]]:
            continue
        waiting_for = (
            sorted({dep.number for dep in tracker.blocked_by(issue.number) if dep.state != "closed"})
            if state == "queued" else []
        )
        has_phase = active
        row.update(
            state=state, issue=issue.number, title=issue.title,
            phase=run.phase if has_phase else None,
            elapsed_seconds=_age(run.phase_started_at, now) if has_phase else None,
            last_activity_seconds=_age(run.last_activity_at or run.started_at, now) if has_phase else None,
            waiting_for=waiting_for,
        )
    return {slug: lanes[slug] for slug in sorted(lanes, key=lane_order)}, summary

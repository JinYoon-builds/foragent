"""Lane visibility derived from the local issue snapshot and persisted runner activity.

Status never reads the tracker: the gate refreshes the snapshot, and Foragent's
own changes are recorded locally as they happen. A missing or failed read is
reported as unknown, never as an empty queue.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Any

from foragent.config import Config
from foragent.labels import (APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, WAITING_PERSON, issue_lane,
                             lane_display, lane_order)
from foragent.reasons import parse_reason
from foragent.state import StateStore
from foragent.tracker import Issue

GROUPS = (NEEDS_HUMAN, RUNNING, "pr-open", READY, "approved-waiting", "unapproved")
SUMMARY_KEYS = ("working", "blocked", "waiting_human", "queued")


def _age(timestamp: str | None, now: datetime) -> float | None:
    return max(0, (now - datetime.fromisoformat(timestamp)).total_seconds()) if timestamp else None


def local_group(row: dict[str, Any]) -> str:
    """The gate status grouping, using the PR recorded in the local snapshot."""
    labels = set(row["labels"])
    if NEEDS_HUMAN in labels:
        return NEEDS_HUMAN
    if RUNNING in labels:
        return RUNNING
    if APPROVED in labels and row.get("pr_number") is not None and not row.get("merged"):
        return "pr-open"
    if READY in labels:
        return READY
    return "approved-waiting" if APPROVED in labels else "unapproved"


def _reason(row: dict[str, Any]) -> str | None:
    comments = sorted(row.get("comments") or [],
                      key=lambda comment: (datetime.fromisoformat(comment["created_at"]), comment["id"]))
    return next((reason.value for comment in reversed(comments)
                 if (reason := parse_reason(comment["body"])) is not None), None)


def collect(store: StateStore, config: Config, now: datetime) -> dict[str, Any]:
    """Build the shared status JSON from local records only."""
    observation = store.issues_observation()
    rows = store.local_issues()
    states = {row["number"]: row["state"] for row in rows}
    open_rows = [row for row in rows if row["state"] == "open"]
    groups: dict[str, list[dict[str, Any]]] = {state: [] for state in GROUPS}
    for row in open_rows:
        item: dict[str, Any] = {"number": row["number"], "title": row["title"]}
        group = local_group(row)
        if group == NEEDS_HUMAN:
            item["reason"] = _reason(row)
        groups[group].append(item)
    issues = [Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"])
              for row in open_rows]
    waiting = {row["number"]: sorted({dep["number"] for dep in row.get("blocked_by") or []
                                      if states.get(dep["number"], dep["state"]) != "closed"})
               for row in open_rows}
    titles = {row["number"]: row["title"] for row in rows}
    lanes, summary = collect_lanes(issues, groups, waiting, store, config, now, titles=titles)
    if observation["as_of"] is None:
        # Nothing usable was observed; only local runs are known facts.
        summary = {key: None for key in SUMMARY_KEYS} | {"working": summary["working"] or None}
    return {
        "issues": groups,
        "lanes": lanes,
        "summary": summary,
        "as_of": observation["as_of"],
        "stale": observation["error"] is not None,
        "error": observation["error"],
        "failed_at": observation["failed_at"],
        # Dreams hold no issue. This lists every running record with its pid, whatever its
        # labels or worker, so a view can show a gone worker; working rows follow run_is_live.
        "active_runs": [asdict(run) for run in store.active_runs() if run.kind == "issue"],
        "history": [asdict(run) for run in store.read_history()[-10:]],
    }


def unknown(status: dict[str, Any]) -> bool:
    """Whether the view must warn that its numbers may be wrong."""
    return bool(status.get("stale")) or status.get("as_of", "") is None


def summary_values(summary: dict[str, Any]) -> dict[str, Any]:
    """Show an unknown count as '?' so it cannot be read as zero."""
    return {key: "?" if value is None else value for key, value in summary.items()}


def issue_state(labels: Any, active: bool, group: str | None = None) -> str | None:
    """One issue's row state from its local facts, the same for status and the lane view.

    A person (needs-human or waiting-person) comes first, then blocked, then a running
    worker; an open PR waits for a person; approved or ready work is queued. None means
    the issue is not shown.
    """
    labels = set(labels or ())
    if {NEEDS_HUMAN, WAITING_PERSON} & labels:
        return "waiting-human"
    if BLOCKED in labels:
        return "blocked"
    if active or RUNNING in labels:
        return "working"
    if group == "pr-open":
        return "waiting-human"
    if APPROVED in labels or READY in labels:
        return "queued"
    return None


def collect_lanes(
    issues: list[Issue], groups: dict[str, list[dict[str, Any]]], waiting: dict[int, list[int]],
    store: StateStore, config: Config, now: datetime, *, titles: dict[int, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Keep queue entries while choosing the most urgent visible item per lane."""
    from foragent.scheduler_stop import run_is_live

    lanes: dict[str, dict[str, Any]] = {}
    summary = dict(working=0, blocked=0, waiting_human=0, queued=0)
    priority = {"working": 0, "waiting-human": 1, "blocked": 2, "queued": 3, "idle": 4}
    grouped = {item["number"]: (group, item) for group, items in groups.items() for item in items}
    # A lane dreams while its dream record runs; the tick owns that record's liveness.
    dreaming = {run.lane for run in store.dream_runs() if run.status == "running"}

    def lane_row(slug: str) -> dict[str, Any]:
        return lanes.setdefault(slug, {
            "slug": slug, "lane_display": lane_display(config, slug, config.language),
            "state": "idle", "issue": None, "title": None, "phase": None,
            "elapsed_seconds": None, "last_activity_seconds": None, "waiting_for": [],
            RUNNING: [], READY: [], "queued": [], "held": [],
            "dreaming": slug in dreaming,
        })

    def show(row: dict[str, Any], state: str, number: int, title: str | None, run, waiting_for) -> None:
        if priority[state] >= priority[row["state"]]:
            return
        row.update(
            state=state, issue=number, title=title,
            phase=run.phase if run else None,
            elapsed_seconds=_age(run.phase_started_at, now) if run else None,
            last_activity_seconds=_age(run.last_activity_at or run.started_at, now) if run else None,
            waiting_for=waiting_for,
        )

    for slug in {*config.lanes, "main"}:
        lane_row(slug)
    for issue in issues:
        run = store.get_run(issue.number)
        active = run_is_live(run)  # The same rule as fa orch stop and fa orch park (#435), labels aside.
        slug = run.lane if active else issue_lane(issue.labels, config.lanes)
        row = lane_row(slug)
        group, item = grouped[issue.number]
        if group in {RUNNING, READY}:
            row[group].append(item)
        # A running label without a live run is stale, not working.
        state = issue_state(set(issue.labels) - {RUNNING}, active, group)
        if state is None:
            continue
        summary[state.replace("-", "_")] += 1
        if state == "queued":
            row["queued"].append(item)
        elif state in {"blocked", "waiting-human"} and not active:
            # Every held issue, not only the lane's most urgent one, has a lane row.
            row["held"].append({**item, "state": state})
        show(row, state, issue.number, issue.title, run if active else None,
             waiting.get(issue.number, []) if state == "queued" else [])
    # Runs recorded locally are working even when the snapshot predates them or is missing.
    seen = {issue.number for issue in issues}
    for run in store.active_runs():
        if run.kind != "issue" or run.number in seen or not run_is_live(run):  # A dream is no issue row.
            continue
        summary["working"] += 1
        show(lane_row(run.lane), "working", run.number, (titles or {}).get(run.number), run, [])
    return {slug: lanes[slug] for slug in sorted(lanes, key=lane_order)}, summary

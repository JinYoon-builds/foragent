"""CI job durations against their job limits (#2497).

GitHub cancels a job that runs past its ``timeout-minutes``. Shard times drift up run by run, so
the facts show each CI job's recent durations from main and merge-train runs beside its limit,
and two kinds alarm the orchestrator before the limit cuts a job:
``ci_job_near_limit`` (the newest duration reaches ``NEAR`` of the limit) and ``ci_job_rising``
(durations rise steadily over recent runs while still under that line).

Writer: the gate tick (``observe``) is the only writer of ``ci-durations.json`` in the state root.
It reads GitHub at most every ``REFRESH``, merges what it lists into the recorded runs (never
replacing them with one listing), fetches the jobs of a run once (a completed run never changes)
and reads each workflow's limits from the base branch. `fa orch facts` reads only that
record (``jobs``), so the facts stay one frozen local read.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from foragent.orch_facts_resources import _time
from foragent.state import StateStore
from foragent.tracker import WORKFLOW_RUNS_PAGE

RECORD = "ci-durations.json"
REFRESH = timedelta(minutes=15)
# The newest measured durations of a job that the facts show and judge; runs are kept until each
# job has this many.
WINDOW = 6
# Pages of the whole run listing searched for train runs, and new runs whose jobs one refresh reads.
TRAIN_PAGES = 5
JOB_READS = 40
# A job alarms once its newest duration reaches this share of its limit (inclusive).
NEAR = (4, 5)
# A rise needs this many measured runs, the newer half's median this factor of the older half's, and
# every newer-half duration above every older-half one.
TREND_RUNS = 4
RISE = 1.15
# GitHub's limit for a job without ``timeout-minutes``.
DEFAULT_LIMIT_MINUTES = 360
TRAIN_PREFIX = "foragent/batch-merge-"
KINDS = ("ci_job_near_limit", "ci_job_rising")


def _measured_branch(branch: Any, base: str) -> bool:
    return isinstance(branch, str) and (branch == base or branch.startswith(TRAIN_PREFIX))


def _read(store: StateStore) -> dict[str, Any]:
    try:
        data = json.loads((store.root / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _workflow_text(root: Path, base: str, path: str) -> str | None:
    """A workflow file as the base branch holds it; the checkout's copy when git cannot say."""
    try:
        return subprocess.run(["git", "-C", str(root), "show", f"origin/{base}:{path}"], check=True,
                              capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return (Path(root) / path).read_text(encoding="utf-8")
    except OSError:
        return None


def workflow_limits(text: str | None) -> dict[str, dict[str, Any]]:
    """Each job of a workflow: its display name and its ``timeout-minutes``."""
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    limits: dict[str, dict[str, Any]] = {}
    for key, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
        job = job if isinstance(job, dict) else {}
        minutes = job.get("timeout-minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
            minutes = DEFAULT_LIMIT_MINUTES
        name = job.get("name") if isinstance(job.get("name"), str) else str(key)
        limits[str(key)] = {"name": name, "limit_minutes": minutes}
    return limits


def _limit(limits: dict[str, dict[str, Any]], job: str) -> float | None:
    """The limit of a job as GitHub names it; a matrix job ``acceptance`` runs as ``acceptance (0)``."""
    for entry in limits.values():
        if job == entry["name"] or job.startswith(entry["name"] + " ("):
            return entry["limit_minutes"]
    return None


def _job_seconds(job: dict[str, Any]) -> float | None:
    started, completed = _time(job.get("started_at")), _time(job.get("completed_at"))
    if started is None or completed is None or completed < started:
        return None
    return (completed - started).total_seconds()


def _order(run: dict[str, Any]) -> tuple[str, str]:
    return str(run.get("started_at") or ""), str(run.get("id"))


def _listed(tracker, base: str, newest_known: str) -> dict[Any, dict[str, Any]]:
    """Completed main and train runs GitHub lists now, by id.

    Each source is filtered before it is cut: GitHub filters main runs by branch before paging,
    and train branches (many names) are found by paging the whole listing until a page reaches
    runs older than the newest one already recorded, or ``TRAIN_PAGES`` pages.
    """
    found: dict[Any, dict[str, Any]] = {}

    def add(run: Any) -> None:
        if isinstance(run, dict) and run.get("status") == "completed" and "id" in run \
                and _measured_branch(run.get("head_branch"), base):
            found[run["id"]] = run

    for run in tracker.recent_workflow_runs(branch=base):
        add(run)
    for page in range(1, TRAIN_PAGES + 1):
        batch = tracker.recent_workflow_runs(page=page)
        for run in batch:
            add(run)
        oldest = min((str(run.get("run_started_at") or run.get("created_at") or "") for run in batch
                      if isinstance(run, dict)), default="")
        if len(batch) < WORKFLOW_RUNS_PAGE or (newest_known and oldest and oldest <= newest_known):
            break
    return found


def _kept(runs: list[dict[str, Any]], limits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The runs that hold a job's newest ``WINDOW`` measured durations, newest first.

    Retention follows the same filter as ``jobs``: a job skipped or cancelled early in some runs
    keeps older runs until it has its window, so no cut happens before the filter.
    """
    counts: dict[tuple[str, str], int] = {}
    kept = []
    for run in sorted(runs, key=_order, reverse=True):
        workflow, needed = run.get("workflow") or "?", False
        for job in run.get("jobs") or []:
            key = (workflow, job["name"])
            if _full_duration(job, _limit(limits.get(workflow) or {}, job["name"])) and counts.get(key, 0) < WINDOW:
                counts[key] = counts.get(key, 0) + 1
                needed = True
        if needed:
            kept.append(run)
    return kept


def observe(store: StateStore, tracker, repo_root: Path, base: str, now: datetime) -> None:
    """Add the runs GitHub lists to the recorded ones; a tick step, so failures propagate to it.

    The record is merged, never replaced by one listing: a run once measured stays until newer
    measured runs of each of its jobs fill that job's window. At most ``JOB_READS`` new runs have
    their jobs read per refresh, newest first; the rest are still listed at the next refresh.
    """
    record = _read(store)
    fetched = _time(record.get("fetched_at"))
    if fetched is not None and fetched <= now < fetched + REFRESH:
        return
    if getattr(tracker, "recent_workflow_runs", None) is None:
        return
    runs = {run["id"]: run for run in record.get("runs") or [] if isinstance(run, dict) and "id" in run}
    newest_known = max((str(run.get("started_at") or "") for run in runs.values()
                        if str(run.get("branch") or "").startswith(TRAIN_PREFIX)), default="")
    listed = _listed(tracker, base, newest_known)
    # Runs read before but not kept (no measured job of theirs is needed) are not read again while listed.
    read = {run_id for run_id in record.get("read_ids") or [] if run_id in listed}
    new = sorted((run for run_id, run in listed.items() if run_id not in runs and run_id not in read),
                 key=lambda run: (str(run.get("run_started_at") or run.get("created_at") or ""), str(run["id"])),
                 reverse=True)
    for run in new[:JOB_READS]:
        read.add(run["id"])
        jobs = []
        for job in tracker.workflow_run_jobs(run["id"]):
            seconds = _job_seconds(job)
            if isinstance(job.get("name"), str) and seconds is not None:
                jobs.append({"name": job["name"], "seconds": seconds, "conclusion": job.get("conclusion")})
        runs[run["id"]] = {"id": run["id"], "workflow": run.get("path") or run.get("name") or "?",
                           "branch": run["head_branch"],
                           "started_at": run.get("run_started_at") or run.get("created_at"), "jobs": jobs}
    limits = {path: workflow_limits(_workflow_text(Path(repo_root), base, path))
              for path in sorted({run.get("workflow") or "?" for run in runs.values()})}
    kept = _kept(list(runs.values()), limits)
    kept_ids = {run["id"] for run in kept}
    value = {"fetched_at": now.isoformat(), "base": base, "runs": kept,
             "read_ids": sorted((run_id for run_id in read if run_id not in kept_ids), key=str),
             "limits": {path: limits[path] for path in sorted({run.get("workflow") or "?" for run in kept})}}
    with store._state_lock():
        store._write_json(store.root / RECORD, value)


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _rising(series: list[float]) -> bool:
    if len(series) < TREND_RUNS:
        return False
    half = len(series) // 2
    older, newer = median(series[:half]), series[-half:]
    # Steady: every newer duration is above every older one, not a noisy series that ends high.
    return older > 0 and median(newer) >= RISE * older and min(newer) > max(series[:half])


def _near(seconds: float, limit: float | None) -> bool:
    return limit is not None and seconds * NEAR[1] >= limit * 60 * NEAR[0]


def _full_duration(job: dict[str, Any], limit: float | None) -> bool:
    """Whether a job's time says how long it takes: a skipped job never ran, and a job cancelled
    early (a newer push replaced its run) stopped short; one cancelled at its limit is the evidence."""
    if job.get("conclusion") == "skipped":
        return False
    return job.get("conclusion") != "cancelled" or _near(float(job["seconds"]), limit)


def jobs(store: StateStore) -> list[dict[str, Any]]:
    """Each measured CI job: its newest durations (oldest first) beside its limit."""
    record = _read(store)
    series: dict[tuple[str, str], list[float]] = {}
    for run in sorted((run for run in record.get("runs") or [] if isinstance(run, dict)),
                      key=lambda run: (str(run.get("started_at") or ""), str(run.get("id")))):
        workflow = run.get("workflow") or "?"
        for job in run.get("jobs") or []:
            limit = _limit((record.get("limits") or {}).get(workflow) or {}, job["name"])
            if not _full_duration(job, limit):
                continue
            series.setdefault((workflow, job["name"]), []).append(float(job["seconds"]))
    result = []
    for (workflow, name), seconds in sorted(series.items()):
        limit = _limit((record.get("limits") or {}).get(workflow) or {}, name)
        recent = seconds[-WINDOW:]
        minutes = [_minutes(value) for value in recent]
        latest = recent[-1]
        near = _near(latest, limit)
        result.append({"job": name, "workflow": workflow, "limit_minutes": limit, "durations_minutes": minutes,
                       "latest_minutes": minutes[-1],
                       "percent_of_limit": None if limit is None else round(100 * latest / (limit * 60)),
                       "near_limit": near, "rising": not near and _rising(recent)})
    return result


def kind_numbers(measured: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The two alarm kinds' numbers, holding only the jobs that meet them."""
    numbers: dict[str, dict[str, Any]] = {}
    for kind, flag in (("ci_job_near_limit", "near_limit"), ("ci_job_rising", "rising")):
        hit = {job["job"]: {"workflow": job["workflow"], "limit_minutes": job["limit_minutes"],
                            "durations_minutes": job["durations_minutes"], "latest_minutes": job["latest_minutes"],
                            "percent_of_limit": job["percent_of_limit"]}
               for job in measured if job[flag]}
        numbers[kind] = {"jobs": hit} if hit else {}
    return numbers

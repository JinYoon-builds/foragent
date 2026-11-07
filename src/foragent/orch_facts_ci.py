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
from itertools import product
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
# The bound on pages of the whole run listing one refresh searches for train runs, and the new runs
# whose jobs one refresh reads.
SEARCH_PAGES = 20
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
WORKFLOWS = ".github/workflows"
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
        name = entry["name"]
        if "${{" in name:  # GitHub renders this name at run time; its static start names the job.
            if job.startswith(name.split("${{", 1)[0].rstrip()):
                return entry["limit_minutes"]
        elif job == name or job.startswith(name + " ("):
            return entry["limit_minutes"]
    return None


def _job_seconds(job: dict[str, Any]) -> float | None:
    started, completed = _time(job.get("started_at")), _time(job.get("completed_at"))
    if started is None or completed is None or completed < started:
        return None
    return (completed - started).total_seconds()


def _order(run: dict[str, Any]) -> tuple[str, str]:
    return str(run.get("started_at") or ""), str(run.get("id"))


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


def _run_started(run: dict[str, Any]) -> str:
    return str(run.get("run_started_at") or run.get("created_at") or "")


def _workflow_paths(root: Path) -> list[str]:
    """The workflow files of the checkout. Read from the folder, so a project without workflows
    starts no process; each file's text and limits still come from the base branch."""
    folder = Path(root) / WORKFLOWS
    try:
        paths = sorted(path for path in folder.iterdir() if path.suffix in {".yml", ".yaml"} and path.is_file())
    except OSError:
        return []
    return [f"{WORKFLOWS}/{path.name}" for path in paths]


def workflow_targets(text: str | None) -> list[dict[str, Any]]:
    """The jobs a workflow runs, one per job and matrix shard, as GitHub names them.

    GitHub's matrix rules (docs: "Running variations of jobs in a workflow"): the product of the
    list axes, minus ``exclude`` rows, then each ``include`` row is added to every combination it
    can join without overwriting an original axis value, or becomes a new combination when it
    joins none (an include-only matrix is just its rows). A shard runs as ``name (v1, v2, ...)``.
    Where GitHub decides the name at run time, the target is a prefix: a matrix or job name built
    from an expression, and a reusable-workflow job (``uses:``, whose jobs run as ``name / job``).
    """
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    targets = []
    for key, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
        job = job if isinstance(job, dict) else {}
        name = job.get("name") if isinstance(job.get("name"), str) else str(key)
        if "${{" in name:
            targets.append({"name": name.split("${{", 1)[0].rstrip(), "prefix": ""})
            continue
        suffix = " / " if "uses" in job else None
        strategy = job.get("strategy") if isinstance(job.get("strategy"), dict) else {}
        matrix = strategy.get("matrix")
        if matrix is None:
            targets.append({"name": name, "prefix": suffix})
            continue
        combos = _combinations(matrix)
        if combos is None:
            targets.append({"name": name, "prefix": " ("})
            continue
        for combo in combos:
            shard = f"{name} ({', '.join(_shown(v) for v in combo.values())})" if combo else name
            targets.append({"name": shard, "prefix": suffix})
    return targets


def _combinations(matrix: Any) -> list[dict[str, Any]] | None:
    """The matrix's combinations in GitHub's order; None when GitHub computes them at run time."""
    if not isinstance(matrix, dict):
        return None
    axes = {axis: values for axis, values in matrix.items() if axis not in {"include", "exclude"}}
    include, exclude = matrix.get("include", []), matrix.get("exclude", [])
    if any(not isinstance(values, list) for values in axes.values()) \
            or not isinstance(include, list) or not isinstance(exclude, list) \
            or not all(isinstance(row, dict) for row in [*include, *exclude]):
        return None
    combos = [dict(zip(axes, values)) for values in product(*axes.values())] if axes else []
    combos = [combo for combo in combos
              if not any(row and all(combo.get(k) == v for k, v in row.items()) for row in exclude)]
    originals = [dict(combo) for combo in combos]
    added = []
    for row in include:
        joined = False
        for combo, original in zip(combos, originals):
            if all(original[k] == v for k, v in row.items() if k in original):
                combo.update(row)
                joined = True
        if not joined:
            added.append(dict(row))
    return combos + added


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _found(job: dict[str, Any]) -> bool:
    """A run finds a job only where the job completed with its times: a skipped job never ran and a
    cancelled one stopped short, so neither says how long the job takes now."""
    return job.get("seconds") is not None and job.get("conclusion") not in {"skipped", "cancelled", None}


def _matches(target: dict[str, Any], job: str) -> bool:
    prefix = target.get("prefix")
    return job == target["name"] or (prefix is not None and job.startswith(target["name"] + prefix))


def _unfound(targets: dict[str, list[dict[str, Any]]], runs: list[dict[str, Any]], older_than: str,
             *, searching: bool = True) -> list[str]:
    """The targets whose latest finding run may still be older than ``older_than``: a target found at
    or after it cannot have a newer run further down a newest-first listing.

    While ``searching``, a target GitHub names at run time (a runtime matrix, an expression name, a
    reusable workflow's jobs) is never found: the shards it runs cannot be known from the pages read
    so far, since one may appear only further down. Its listing therefore reads to the listing's
    end or the reported page bound.
    """
    latest: dict[tuple[str, str], str] = {}
    for run in runs:
        workflow = run.get("workflow") or "?"
        for job in run.get("jobs") or []:
            if not _found(job):
                continue
            for target in targets.get(workflow) or []:
                if _matches(target, job["name"]):
                    key = (workflow, target["name"])
                    latest[key] = max(latest.get(key, ""), str(run.get("started_at") or ""))
    return sorted(f"{workflow}: {target['name']}" for workflow, rows in targets.items() for target in rows
                  if (searching and target.get("prefix") is not None)
                  or not latest.get((workflow, target["name"])) or latest[(workflow, target["name"])] < older_than)


def _train_branches(root: Path) -> list[str]:
    """Train branches the remote holds now; their runs are asked for by branch name."""
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-remote", "--heads", "origin", f"refs/heads/{TRAIN_PREFIX}*"],
                                check=True, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split("refs/heads/", 1)[1] for line in listed.splitlines() if "refs/heads/" in line})


def observe(store: StateStore, tracker, repo_root: Path, base: str, now: datetime) -> None:
    """Add the measured runs GitHub lists to the recorded ones; a tick step, so failures propagate to it.

    The record is merged, never replaced by one listing: a run once measured stays until newer
    measured runs of each of its jobs fill that job's window. GitHub is asked only for measured runs,
    filtered before paging, per workflow file on the base branch: its runs on the base branch, its
    push runs (pushed train branches), and its runs on each train branch the remote holds. PR runs
    of other branches therefore never fill those pages. Each listing pages, newest first, until every
    target (each job and matrix shard of the workflow) has a run that completed it with times at
    least as new as the page reached, the listing ends, or ``SEARCH_PAGES`` pages, which is the
    bound ``search`` reports. At most ``JOB_READS`` new runs have their jobs read per refresh; the
    rest are read at the next refresh, since runs read before are not read again.
    """
    record = _read(store)
    fetched = _time(record.get("fetched_at"))
    if fetched is not None and fetched <= now < fetched + REFRESH:
        return
    if getattr(tracker, "recent_workflow_runs", None) is None:
        return
    root = Path(repo_root)
    runs = {run["id"]: run for run in record.get("runs") or [] if isinstance(run, dict) and "id" in run}
    known_read = set(record.get("read_ids") or [])
    read: set[Any] = set()
    budget = [JOB_READS]
    texts = {path: _workflow_text(root, base, path) for path in _workflow_paths(root)}
    limits = {path: workflow_limits(text) for path, text in texts.items()}
    targets = {path: rows for path, rows in ((path, workflow_targets(text)) for path, text in texts.items()) if rows}

    def take(batch: list[Any]) -> bool:
        """Read the jobs of the batch's new main and train runs, newest first; False once the
        refresh's reads are spent with runs left unread."""
        measured = sorted((run for run in batch if isinstance(run, dict) and run.get("status") == "completed"
                           and "id" in run and _measured_branch(run.get("head_branch"), base)),
                          key=lambda run: (_run_started(run), str(run["id"])), reverse=True)
        for run in measured:
            if run["id"] in runs or run["id"] in read:
                continue
            if run["id"] in known_read:
                read.add(run["id"])
                continue
            if budget[0] <= 0:
                return False
            budget[0] -= 1
            read.add(run["id"])
            jobs = []
            for job in tracker.workflow_run_jobs(run["id"]):
                seconds = _job_seconds(job)
                if isinstance(job.get("name"), str) and seconds is not None:
                    jobs.append({"name": job["name"], "seconds": seconds, "conclusion": job.get("conclusion")})
            runs[run["id"]] = {"id": run["id"], "workflow": run.get("path") or run.get("name") or "?",
                               "branch": run["head_branch"], "started_at": _run_started(run) or None, "jobs": jobs}
        return True

    # Train runs are often pull_request runs, which only a branch filter separates from PR traffic.
    # A train branch is remembered while the remote holds it and listed once more after it is gone,
    # so the runs that finished just before it landed are read too.
    live = set(_train_branches(root)) if targets else set()
    trains = sorted(live | {name for name in record.get("train_branches") or [] if isinstance(name, str)})
    listings = []
    for workflow in sorted(targets):
        # Main runs by branch; train runs by event (a train is a push or a pull request, and its branch
        # may already be gone) and by the names of train branches seen. Each listing pages until
        # every target is found, so pull_request traffic costs pages, never a missed train run.
        sources = [{"branch": base}, {"event": "push"}, {"event": "pull_request"},
                   *({"branch": branch} for branch in trains)]
        for source in sources:
            pages, stop = 0, "reads"
            while True:
                if pages >= SEARCH_PAGES:
                    stop = "page_limit"
                    break
                pages += 1
                batch = tracker.recent_workflow_runs(page=pages, workflow=workflow, **source)
                if not take(batch):
                    break
                oldest = min((_run_started(run) for run in batch if isinstance(run, dict) and _run_started(run)),
                             default="")
                if not _unfound({workflow: targets[workflow]}, list(runs.values()), oldest):
                    stop = "found"
                    break
                if len(batch) < WORKFLOW_RUNS_PAGE:
                    stop = "listing_end"
                    break
            listings.append({"workflow": workflow, **source, "pages": pages, "stopped": stop})
    unread = {row["branch"] for row in listings if row.get("branch") in trains and row["stopped"] == "reads"}
    for path in sorted({run.get("workflow") or "?" for run in runs.values()} - set(limits)):
        limits[path] = workflow_limits(_workflow_text(root, base, path))
    kept = _kept(list(runs.values()), limits)
    kept_ids = {run["id"] for run in kept}
    value = {"fetched_at": now.isoformat(), "base": base, "runs": kept,
             "read_ids": sorted((run_id for run_id in read if run_id not in kept_ids), key=str),
             "limits": {path: limits[path] for path in sorted({run.get("workflow") or "?" for run in kept})},
             "train_branches": sorted(live | unread),
             "search": {"page_limit": SEARCH_PAGES, "listings": listings,
                        "never_found_jobs": _unfound(targets, list(runs.values()), "", searching=False)}}
    with store._state_lock():
        store._write_json(store.root / RECORD, value)


def search(store: StateStore) -> dict[str, Any] | None:
    """How the last refresh searched: each server-filtered listing (workflow, branch or event, pages
    read, and whether it stopped ``found``, at ``listing_end``, at the ``page_limit`` bound, or on the
    refresh's job ``reads``), plus the targets no recorded run has completed (``never_found_jobs``)."""
    value = _read(store).get("search")
    return value if isinstance(value, dict) else None


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

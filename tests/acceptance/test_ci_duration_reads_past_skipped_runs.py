"""Acceptance tests for ci-duration-reads-past-skipped-runs: the CI duration facts find a job's
latest main or train run even when many newer runs skip that job.

Public entry points only: `fa gate tick --no-start` observes CI and raises alarms, `fa orch inbox
--json` shows what the orchestrator receives, `fa orch facts --json` shows the measured facts.
GitHub is the offline FakeTracker; no process is started or signalled.

Contract reused from #2497 (tests/acceptance/test_shard_duration_alarm.py): recent workflow runs
reach the tracker through ``FakeTracker.set_workflow_runs(runs)``, newest first, each GitHub REST
run carrying its REST jobs under ``jobs``; the tracker lists them in pages of
``tracker.WORKFLOW_RUNS_PAGE`` (100) as GitHub does. A job's limit is its ``timeout-minutes`` on
main. Only main and merge-train (``foragent/batch-merge-*``) runs are measured; a job skipped in a
run has no duration there.

The statements do not say how many refreshes a reading may take, so each test runs up to three
gate ticks, each past the 15-minute refresh of #2497; the first tick starts from no record
(startup).

Break-it: stopping the train search at the newest recorded train run (after a 40-run job-read cut
on the first tick), a fixed five-page train search, reading the skipped acceptance job as a
duration, or an alarm repeated on each tick each fail a check below.
"""

from datetime import timedelta
import json

from test_shard_duration_alarm import (  # noqa: F401
    about, assert_alarm, cli_exit, evidence, facts, feed, gate_requests, inbox, isolated_home,
    job_views, numbers, says_minutes, stamp, tick_case,
)
from test_tick_failure_alert import run_tick
from conftest import git_command


WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW = """name: ci
on:
  push:
    branches: [main, "foragent/batch-merge-*"]
  pull_request:
jobs:
  acceptance:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    strategy:
      matrix:
        shard: [0]
    steps:
      - run: echo acceptance ${{ matrix.shard }}
  unit:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - run: echo unit
"""
ACCEPTANCE, UNIT = "acceptance (0)", "unit"
ACCEPTANCE_LIMIT, UNIT_LIMIT = 45, 30
TICKS = 3


def install_workflow(case):
    case.history.update({WORKFLOW_PATH: WORKFLOW}, message="CI workflow")
    git_command(case.root, "fetch", "origin")
    path = case.root / WORKFLOW_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW, encoding="utf-8")


def run(case, index, branch, jobs, *, age_minutes):
    """One completed REST workflow run that started ``age_minutes`` before the case clock.
    ``jobs`` maps a job name to its minutes, or to None when the run skipped the job."""
    started = case.clock[0] - timedelta(minutes=age_minutes)
    rest_jobs, finished = [], started
    for j, (name, minutes) in enumerate(jobs.items()):
        end = started if minutes is None else started + timedelta(minutes=minutes)
        finished = max(finished, end)
        rest_jobs.append({"id": 900000 + 10 * index + j, "run_id": 70000 + index, "name": name,
                          "status": "completed", "conclusion": "skipped" if minutes is None else "success",
                          "head_branch": branch, "started_at": stamp(started), "completed_at": stamp(end)})
    pull = branch.startswith("foragent/issue-")
    return {"id": 70000 + index, "name": "ci", "path": WORKFLOW_PATH, "workflow_id": 77,
            "head_branch": branch, "head_sha": f"{index:040x}",
            "event": "pull_request" if pull else "push", "status": "completed", "conclusion": "success",
            "created_at": stamp(started), "run_started_at": stamp(started),
            "updated_at": stamp(finished), "jobs": rest_jobs}


def ticks(case, capsys):
    for _ in range(TICKS):
        case.clock[0] += timedelta(minutes=16)  # Past the 15-minute refresh; run_tick adds 5 more.
        run_tick(case, capsys)


def acceptance_numbers(case, capsys):
    data = facts(case, capsys)
    views = job_views(data, ACCEPTANCE)
    assert views, (f"fa orch facts must show {ACCEPTANCE} measured from main and train runs: "
                   f"{json.dumps(data)[:2000]}")
    return views, set().union(*(numbers(view) for view in views))


# ---------------------------------------------------------------- statement 1


def skipped_ahead_runs(case):
    """Newest first: 100 runs (40 train runs that skip acceptance, 60 PR runs, mixed), then the
    latest acceptance train run at 40 of 45 minutes, then six older main runs at 20 minutes."""
    runs, train, pull = [], 0, 0
    for i in range(100):
        if i % 5 < 2:
            branch = f"foragent/batch-merge-{3000 + train}"
            train += 1
            jobs = {ACCEPTANCE: None, UNIT: 12}
        else:
            branch = f"foragent/issue-{5000 + pull}"
            pull += 1
            jobs = {ACCEPTANCE: 9, UNIT: 12}
        runs.append(run(case, i, branch, jobs, age_minutes=60 + 5 * i))
    assert (train, pull) == (40, 60)
    runs.append(run(case, 100, "foragent/batch-merge-2999", {ACCEPTANCE: 40, UNIT: 12}, age_minutes=60 + 5 * 100))
    for k in range(6):
        runs.append(run(case, 101 + k, "main", {ACCEPTANCE: 20, UNIT: 12}, age_minutes=60 + 5 * 101 + 120 * k))
    return runs


def test_acceptance_train_behind_skipping_runs_is_measured_and_alarms(tick_case, capsys):
    case = tick_case
    install_workflow(case)
    feed(case, skipped_ahead_runs(case))
    ticks(case, capsys)

    views, found = acceptance_numbers(case, capsys)
    assert says_minutes(found, 40), (
        f"The latest acceptance train run (40 of 45 minutes) sits behind 40 newer train runs that skip "
        f"acceptance and 60 newer PR runs; the facts must still report its duration: {views}")
    assert not says_minutes(found, 9), f"PR runs (9 min) are not main or train runs and are not measured: {views}"

    requests = gate_requests(case, capsys)
    alarms = [row for row in requests if about(row, ACCEPTANCE)]
    assert len(alarms) == 1, (
        f"{ACCEPTANCE} at 40 of its 45 minutes (89%) must give the orchestrator exactly one time-limit "
        f"alarm across {TICKS} ticks; gate requests: {[evidence(row) for row in requests]}")
    assert_alarm(alarms[0], ACCEPTANCE, [40], ACCEPTANCE_LIMIT)
    assert not [row for row in requests if about(row, UNIT)], (
        f"{UNIT} runs 12 of its 30 minutes, flat: no alarm may name it: {[evidence(r) for r in requests]}")


# ---------------------------------------------------------------- statement 2


def startup_runs(case):
    """Newest first: 500 PR runs, then the latest train run (acceptance 17 min), then six older
    main runs (acceptance 11 min)."""
    runs = [run(case, i, f"foragent/issue-{6000 + i}", {ACCEPTANCE: 9, UNIT: 12}, age_minutes=30 + i)
            for i in range(500)]
    runs.append(run(case, 500, "foragent/batch-merge-4000", {ACCEPTANCE: 17, UNIT: 12}, age_minutes=30 + 500))
    for k in range(6):
        runs.append(run(case, 501 + k, "main", {ACCEPTANCE: 11, UNIT: 12}, age_minutes=30 + 501 + 120 * k))
    return runs


def test_train_job_behind_500_pr_runs_is_measured_at_startup(tick_case, capsys):
    case = tick_case
    install_workflow(case)
    feed(case, startup_runs(case))
    ticks(case, capsys)

    views, found = acceptance_numbers(case, capsys)
    assert says_minutes(found, 17), (
        f"At startup the latest train run (acceptance 17 min) sits behind 500 newer PR runs; the facts "
        f"must still report that train job's duration, not only the older main runs (11 min): {views}")
    assert says_minutes(found, 11), f"The older main runs stay measured too: {views}"
    assert not says_minutes(found, 9), f"PR runs (9 min) are not main or train runs and are not measured: {views}"

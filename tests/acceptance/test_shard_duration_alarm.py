"""Acceptance tests for shard-duration-alarm: slow CI jobs raise an alarm before their job limit cuts them.

Only public entry points are driven: `fa gate tick --no-start` measures and raises, `fa orch inbox
--json` reads what the orchestrator receives, `fa orch facts --json` shows the measured facts.
No process is started or signalled; GitHub is the offline FakeTracker.

Chosen test contract (the only one these tests pin):
- Recent CI runs reach the gate through the tracker. The FakeTracker takes them with
  ``set_workflow_runs(runs)``: a list of GitHub REST "workflow run" objects (newest first, as the
  REST listing returns them), each carrying its REST "job" objects under ``jobs``. A job's
  duration is ``completed_at - started_at``.
- A job's limit is its ``timeout-minutes`` in the workflow file on main (.github/workflows/*.yml).
  A matrix job ``acceptance`` runs on GitHub as ``acceptance (0)``, ``acceptance (1)``.
- Only main and merge-train (``foragent/batch-merge-*``) runs are measured (Goal).

What counts as "the alarm": an inbox request written by the gate (``from: gate``) whose text,
facts or signal names the job. Field names are the lane's choice; the checks search those values
for the job name, its durations and its limit (in minutes or in seconds).

Break-it: a ``>`` instead of ``>=`` at 80%, one limit for every job, a repeated alarm on the next
tick, no alarm, an alarm without durations or limit, a trend alarm on flat durations, counting
issue-branch runs, or facts without the job each fail a check below.
"""

from datetime import timedelta
import json
import re

import pytest

from conftest import git_command
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW = """name: ci
on:
  push:
    branches: [main, "foragent/batch-merge-*"]
  pull_request:
jobs:
  acceptance:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        shard: [0, 1]
    steps:
      - run: echo acceptance ${{ matrix.shard }}
  unit:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - run: echo unit
"""
LIMITS = {"acceptance (0)": 30, "acceptance (1)": 30, "unit": 45}
RUNS = 6
TREND = re.compile(r"ris|trend|upward|increas|grow|climb|drift", re.IGNORECASE)


# ---------------------------------------------------------------- fixture helpers


def install_workflow(case):
    """The workflow file lands on main (origin) and in the checkout."""
    case.history.update({WORKFLOW_PATH: WORKFLOW}, message="CI workflow")
    git_command(case.root, "fetch", "origin")
    path = case.root / WORKFLOW_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW, encoding="utf-8")


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def workflow_runs(case, series, branches=None):
    """GitHub REST workflow runs, newest first. ``series`` maps a job name to its durations in
    minutes, oldest first; run i holds every job's i-th duration. Runs are two hours apart and
    alternate between main pushes and merge-train branches."""
    count = len(next(iter(series.values())))
    branches = branches or ["main" if i % 2 == 0 else f"foragent/batch-merge-{900 + i}" for i in range(count)]
    runs = []
    for i in range(count):
        started = case.clock[0] - timedelta(hours=2 * (count - i)) + timedelta(minutes=5)
        jobs, finished = [], started
        for j, (name, minutes) in enumerate(series.items()):
            end = started + timedelta(minutes=minutes[i])
            finished = max(finished, end)
            jobs.append({"id": 50000 + 10 * i + j, "run_id": 4000 + i, "name": name, "status": "completed",
                         "conclusion": "success", "head_branch": branches[i],
                         "started_at": stamp(started), "completed_at": stamp(end)})
        runs.append({"id": 4000 + i, "name": "ci", "path": WORKFLOW_PATH, "workflow_id": 77,
                     "head_branch": branches[i], "head_sha": f"{i:040x}",
                     "event": "push", "status": "completed", "conclusion": "success",
                     "created_at": stamp(started), "run_started_at": stamp(started),
                     "updated_at": stamp(finished), "jobs": jobs})
    return list(reversed(runs))


def feed(case, runs):
    """Hand the runs to the tracker through the chosen contract. Without the setter (current main)
    the runs stay unread and the behaviour checks below fail on their own assertions."""
    setter = getattr(case.tracker, "set_workflow_runs", None)
    if setter is not None:
        setter(runs)
    else:
        case.tracker.workflow_runs_for_test = runs


def tick(case, capsys):
    run_tick(case, capsys)


def gate_requests(case, capsys):
    return [row for row in inbox(case, capsys) if row.get("from") == "gate"]


def evidence(row):
    return json.dumps({key: row.get(key) for key in ("text", "facts", "signal")}, ensure_ascii=False, default=str)


def about(row, job):
    return job in evidence(row)


def numbers(value, text=""):
    """Every number a value states: numbers in nested data and standalone numbers in text."""
    found = set()

    def walk(item):
        if isinstance(item, bool):
            return
        if isinstance(item, (int, float)):
            found.add(round(float(item), 1))
        elif isinstance(item, dict):
            for inner in item.values():
                walk(inner)
        elif isinstance(item, list):
            for inner in item:
                walk(inner)
        elif isinstance(item, str):
            for match in re.findall(r"(?<![\w#./:-])(\d+(?:\.\d+)?)(?:\s?(?:minutes|minute|mins|min|m|seconds|second|secs|sec|s))?(?![\w/:-])", item):
                found.add(round(float(match), 1))

    walk(value)
    walk(text)
    return found


def row_numbers(row):
    return numbers(row.get("facts"), json.dumps(row.get("signal"), default=str) + " " + (row.get("text") or ""))


def says_minutes(found, minutes):
    """A duration or limit given in minutes or in seconds."""
    return round(float(minutes), 1) in found or round(float(minutes) * 60, 1) in found


def facts(case, capsys):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "facts", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch facts failed: {output.err}"
    return json.loads(output.out)


def job_views(data, job):
    """Parts of the facts about one job: the value under a key naming it, or a mapping naming it."""
    views = []

    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key == job:
                    views.append(value)
                elif value == job:
                    views.append(item)
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(data)
    return views


@pytest.fixture
def ci_case(tick_case):
    install_workflow(tick_case)
    return tick_case


def assert_alarm(row, job, durations, limit):
    text = evidence(row)
    found = row_numbers(row)
    assert row["status"] != "done", f"The orchestrator receives an open request to act on: {text}"
    assert says_minutes(found, limit), f"The alarm must name {job}'s limit ({limit} min): {text}"
    for minutes in durations:
        assert says_minutes(found, minutes), f"The alarm must name {job}'s measured duration {minutes} min: {text}"


# ---------------------------------------------------------------- statement 1: 80% of the limit


@pytest.mark.parametrize("minutes", [25, 24], ids=["over-80-percent", "exactly-80-percent"])
def test_job_at_80_percent_of_its_limit_raises_one_alarm(ci_case, capsys, minutes):
    case = ci_case
    # acceptance (0) takes 24-25 of its 30 minutes; acceptance (1) and unit are far from theirs.
    # unit's 30 minutes would be 100% of a 30-minute limit, but its own limit is 45 (67%).
    feed(case, workflow_runs(case, {"acceptance (0)": [minutes] * RUNS, "acceptance (1)": [10] * RUNS,
                                    "unit": [30] * RUNS}))
    tick(case, capsys)
    tick(case, capsys)  # The same durations on the next tick are the same alarm, not a second one.

    requests = gate_requests(case, capsys)
    alarms = [row for row in requests if about(row, "acceptance (0)")]
    assert len(alarms) == 1, (
        f"A job at {minutes} of its 30 minutes ({minutes / 30:.0%}) must give the orchestrator exactly one "
        f"alarm across two ticks (runs fed through FakeTracker.set_workflow_runs); gate requests: "
        f"{[evidence(row) for row in requests]}")
    assert_alarm(alarms[0], "acceptance (0)", [minutes], 30)
    for job in ("acceptance (1)", "unit"):
        assert not [row for row in requests if about(row, job)], (
            f"{job} is flat and under 80% of its own limit ({LIMITS[job]} min): no alarm may name it: "
            f"{[evidence(row) for row in requests]}")


# ---------------------------------------------------------------- statement 2: upward trend


def test_job_rising_under_its_limit_raises_a_trend_alarm(ci_case, capsys):
    case = ci_case
    rising = [12, 14, 16, 18, 20, 22]  # Oldest first; 22 is 73% of 30, under the 80% line.
    feed(case, workflow_runs(case, {"acceptance (0)": rising, "acceptance (1)": [10] * RUNS,
                                    "unit": [12] * RUNS}))
    tick(case, capsys)
    tick(case, capsys)

    requests = gate_requests(case, capsys)
    alarms = [row for row in requests if about(row, "acceptance (0)")]
    assert len(alarms) == 1, (
        "A job whose duration rises 12 -> 22 minutes over six main and train runs must give the "
        "orchestrator exactly one alarm across two ticks (runs fed through FakeTracker.set_workflow_runs); "
        f"gate requests: {[evidence(row) for row in requests]}")
    row = alarms[0]
    assert TREND.search(evidence(row)), f"The alarm must say the duration rises: {evidence(row)}"
    assert_alarm(row, "acceptance (0)", [22], 30)
    earlier = [minutes for minutes in rising[:-1] if says_minutes(row_numbers(row), minutes)]
    assert earlier, f"The alarm must name the earlier measured durations too, not only the latest: {evidence(row)}"
    for job in ("acceptance (1)", "unit"):
        assert not [item for item in requests if about(item, job)], (
            f"{job} is flat and well under its limit: no alarm may name it: {[evidence(r) for r in requests]}")


# ---------------------------------------------------------------- statement 3: flat and well under


def test_flat_jobs_well_under_their_limits_raise_nothing_and_facts_show_them(ci_case, capsys):
    case = ci_case
    series = {"acceptance (0)": [11] * RUNS, "acceptance (1)": [10] * RUNS, "unit": [12] * RUNS}
    feed(case, workflow_runs(case, series))
    tick(case, capsys)
    tick(case, capsys)

    requests = gate_requests(case, capsys)
    assert not [row for row in requests if any(about(row, job) for job in LIMITS)], (
        f"Flat durations well under the limit raise no alarm: {[evidence(row) for row in requests]}")

    data = facts(case, capsys)
    for job, limit in LIMITS.items():
        views = job_views(data, job)
        assert views, (f"fa orch facts must show each CI job's duration against its limit; {job} is missing "
                       f"(runs fed through FakeTracker.set_workflow_runs): {json.dumps(data)[:2000]}")
        found = set().union(*(numbers(view) for view in views))
        assert says_minutes(found, limit), f"The facts must give {job}'s limit ({limit} min): {views}"
        assert says_minutes(found, series[job][-1]), (
            f"The facts must give {job}'s measured duration ({series[job][-1]} min): {views}")


def test_only_main_and_train_runs_are_measured(ci_case, capsys):
    """Goal: durations come from recent main and train runs. A slow run on an issue branch, here
    the newest run, is no evidence about main's CI."""
    case = ci_case
    runs = workflow_runs(case, {"acceptance (0)": [11] * RUNS, "acceptance (1)": [10] * RUNS, "unit": [12] * RUNS})
    case.clock[0] += timedelta(hours=1)  # The issue-branch run starts an hour after the newest main run.
    slow = workflow_runs(case, {"acceptance (0)": [29], "acceptance (1)": [10], "unit": [12]},
                         branches=["foragent/issue-77"])[0]
    case.clock[0] -= timedelta(hours=1)
    slow["event"] = "pull_request"
    slow["id"] = 4999
    for job in slow["jobs"]:
        job["run_id"] = 4999
        job["id"] += 9000
    feed(case, [slow, *runs])
    tick(case, capsys)

    requests = gate_requests(case, capsys)
    assert not [row for row in requests if any(about(row, job) for job in LIMITS)], (
        f"An issue-branch run is not a main or train run and raises no alarm: {[evidence(r) for r in requests]}")
    data = facts(case, capsys)
    views = job_views(data, "acceptance (0)")
    assert views, f"fa orch facts must show acceptance (0) from the main and train runs: {json.dumps(data)[:2000]}"
    found = set().union(*(numbers(view) for view in views))
    assert says_minutes(found, 11), f"The facts must give the main/train duration (11 min): {views}"
    assert not says_minutes(found, 29), f"The issue-branch run (29 min) must not be measured: {views}"

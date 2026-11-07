"""Shared helpers for ci-rising-raise acceptance tests (2026-11-08).

Only public entry points are driven: `fa gate tick --no-start` measures CI job durations from the
offline FakeTracker's workflow runs and raises role signals, `fa orch inbox --json` reads what the
orchestrator receives and `fa orch signal-answer` answers. No process is started or signalled.

The workflow gives the ``test`` job a 100-minute limit, so a matrix job's newest duration in
minutes is its share of the limit in percent (``percent_of_limit``).

A phase sets each job's whole window of recent durations (``orch_facts_ci.WINDOW`` runs, oldest
first) by appending that many new main-branch runs, then ticks once after the 15-minute refresh.
"""

from datetime import timedelta

from test_tick_failure_alert import inbox, run_tick

WORKFLOW = """name: ci
on: [push]
jobs:
  test:
    timeout-minutes: 100
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
"""


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def rising(latest):
    """A steady rise ending at ``latest`` minutes: every newer half value above the older half."""
    low = max(1, round(latest / 3))
    return [low, low, low, latest - 2, latest - 1, latest]


def flat(minutes=5):
    return [minutes] * 6


def setup(case):
    (case.root / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (case.root / ".github/workflows/ci.yml").write_text(WORKFLOW, encoding="utf-8")
    case.ci_runs = []
    case.ci_next_id = 5000
    case.ci_phase = 0


def phase(case, capsys, jobs):
    """New CI runs whose six newest durations per job are ``jobs[name]``; then one gate tick.

    Runs are stamped in the past hours before the tick, newest last, each holding every job.
    """
    from foragent import orch_facts_ci

    case.ci_phase += 1
    # Past the CI refresh: run_tick adds five minutes to this.
    case.clock[0] += orch_facts_ci.REFRESH + timedelta(minutes=1)
    tick_at = case.clock[0] + timedelta(minutes=5)
    count = len(next(iter(jobs.values())))
    for index in range(count):
        started = tick_at - timedelta(minutes=10 * (count - index))
        run = {"id": case.ci_next_id, "name": "ci", "path": ".github/workflows/ci.yml",
               "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success",
               "run_started_at": stamp(started), "created_at": stamp(started), "jobs": []}
        case.ci_next_id += 1
        for name, series in jobs.items():
            run["jobs"].append({"name": name, "conclusion": "success", "status": "completed",
                                "started_at": stamp(started),
                                "completed_at": stamp(started + timedelta(minutes=series[index]))})
        case.ci_runs.insert(0, run)
    case.tracker.set_workflow_runs(case.ci_runs)
    run_tick(case, capsys)


def rising_requests(case, capsys):
    """Every ci_job_rising request the gate raised, oldest first."""
    return [row for row in inbox(case, capsys)
            if row.get("from") == "gate" and (row.get("signal") or {}).get("kinds") == ["ci_job_rising"]]


def near_requests(case, capsys):
    return [row for row in inbox(case, capsys)
            if row.get("from") == "gate" and (row.get("signal") or {}).get("kinds") == ["ci_job_near_limit"]]


def answer_not_now(case, capsys, request_id):
    from test_skills_autosync import cli_exit

    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "signal-answer", request_id, "not-now", "--why",
                                "Far below the limit", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch signal-answer must answer the CI signal: {output.err}"


def raised_jobs(row):
    """The jobs a raise names in its facts (the field the gate records on the request)."""
    numbers = (((row.get("facts") or {}).get("kinds") or {}).get("ci_job_rising") or {}).get("numbers") or {}
    return set((numbers.get("jobs") or {}).keys())


def first_raise(case, capsys, jobs):
    """One phase that raises ci_job_rising for the first time; the orchestrator answers not-now."""
    phase(case, capsys, jobs)
    rows = rising_requests(case, capsys)
    assert len(rows) == 1, f"The first rise is raised once as today: {rows}"
    answer_not_now(case, capsys, rows[0]["id"])
    return rows[0]

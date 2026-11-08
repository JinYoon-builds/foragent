"""Acceptance: plain `fa orch facts` names CI jobs and their budgets in CI alarms (n100).

Only public entry points run: `fa orch facts` and `fa orch facts --json` over a recorded
``ci-durations.json`` (the gate tick's record, written here directly as recorded facts). No process
is started or signalled (the locked facts fixture forbids it).

Recorded jobs (minutes chosen so no value is a substring of another):
- ``unit (2)``: near its 45-minute limit, latest 41.5 min (ci_job_near_limit).
- ``acceptance (0)``: rising under its 60-minute limit, latest 22.7 min (ci_job_rising).
- ``lint``: rising, its workflow has no recorded limits, latest 8.4 min (ci_job_rising, limit unknown).
- ``docs``: flat, 5.2 min of a 30-minute limit, no alarm (control).
"""

from datetime import timedelta
import json
import re

from test_orch_facts import NOW, facts, facts_case, no_facts_processes  # noqa: F401

CI = ".github/workflows/ci.yml"
LINT = ".github/workflows/lint.yml"

NEAR_JOB, RISING_JOB, UNKNOWN_JOB, FLAT_JOB = "unit (2)", "acceptance (0)", "lint", "docs"
SERIES = {
    NEAR_JOB: (CI, [1800, 1820, 1840, 1860, 1880, 2490]),
    RISING_JOB: (CI, [600, 600, 600, 1200, 1300, 1362]),
    UNKNOWN_JOB: (LINT, [180, 180, 180, 400, 450, 504]),
    FLAT_JOB: (CI, [312] * 6),
}
LIMITS = {CI: {"unit": {"name": "unit", "limit_minutes": 45},
               "acceptance": {"name": "acceptance", "limit_minutes": 60},
               "docs": {"name": "docs", "limit_minutes": 30}}}
EXPECTED = {NEAR_JOB: (41.5, 45, "ci_job_near_limit"), RISING_JOB: (22.7, 60, "ci_job_rising"),
            UNKNOWN_JOB: (8.4, None, "ci_job_rising")}
KIND_WORDS = {"ci_job_near_limit": r"near|ci_job_near_limit", "ci_job_rising": r"ris|ci_job_rising"}


def record(case, names):
    """A ci-durations.json as the gate tick writes it: six runs per workflow, newest first."""
    runs = []
    for index in range(6):
        started = NOW - timedelta(hours=6 - index)
        for workflow in (CI, LINT):
            jobs = [{"name": name, "seconds": float(series[index]), "conclusion": "success"}
                    for name, (where, series) in SERIES.items() if name in names and where == workflow]
            if jobs:
                runs.append({"id": f"{workflow}-{index}", "workflow": workflow, "branch": "main",
                             "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "jobs": jobs})
    runs.sort(key=lambda run: run["started_at"], reverse=True)
    value = {"fetched_at": (NOW - timedelta(minutes=5)).isoformat(), "base": "main", "runs": runs,
             "read_ids": [], "limits": {path: limits for path, limits in LIMITS.items()
                                        if any(run["workflow"] == path for run in runs)}}
    case.store.root.mkdir(parents=True, exist_ok=True)
    (case.store.root / "ci-durations.json").write_text(json.dumps(value), encoding="utf-8")


def number(value):
    """A regex for a number as text: 45 matches "45" or "45.0", never "145" or "45.5"."""
    text = f"{value:g}" if isinstance(value, float) else str(value)
    if "." not in text:
        text += r"(?:\.0+)?"
    else:
        text = text.replace(".", r"\.") + "0*"
    return re.compile(r"(?<![\d.])" + text + r"(?!\.?\d)")


def candidates(line, job, others):
    """The text that may belong to ``job`` on one line: from its name to the next alarm job's name,
    or from the previous alarm job's name to the end of its own name (details before the name)."""
    start = line.index(job)
    end = start + len(job)
    nxt = min((line.find(other, end) for other in others if line.find(other, end) >= 0), default=len(line))
    prev = max((line.rfind(other, 0, start) + len(other) for other in others if line.rfind(other, 0, start) >= 0),
               default=0)
    return [line[start:nxt], line[prev:end]]


def owned(output, job, kind, names, must, must_not=()):
    """The part of a line naming ``job`` with its alarm kind that holds every ``must`` pattern and
    no ``must_not`` pattern (another job's numbers); fails with the whole output otherwise."""
    others = [name for name in names if name != job and job not in name]
    for line in output.splitlines():
        if job not in line or not re.search(KIND_WORDS[kind], line, re.I):
            continue
        for part in candidates(line, job, others):
            if all(p.search(part) for p in must) and not any(p.search(part) for p in must_not):
                return part
    raise AssertionError(
        f"Plain fa orch facts must show the {kind} alarm of {job!r} with "
        f"{[p.pattern for p in must]} and without {[p.pattern for p in must_not]} beside its name:\n{output}")


UNKNOWN = re.compile(r"unknown", re.I)


def test_near_and_rising_alarms_name_their_jobs_latest_and_limit_minutes(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    record(case, {NEAR_JOB, RISING_JOB, FLAT_JOB})
    result = facts(case, capsys)
    assert {"ci_job_near_limit", "ci_job_rising"} <= set(result["kinds"]), (
        f"The recorded durations meet both CI alarm kinds: {result['kinds']}")
    output = facts(case, capsys, human=True)
    names = [NEAR_JOB, RISING_JOB]
    # Each job keeps its own numbers: no swapped limits or latest minutes between the two alarms.
    owned(output, NEAR_JOB, "ci_job_near_limit", names, [number(41.5), number(45)], [number(22.7), number(60)])
    owned(output, RISING_JOB, "ci_job_rising", names, [number(22.7), number(60)], [number(41.5), number(45)])


def test_rising_job_without_a_limit_shows_the_limit_as_unknown(tmp_path, monkeypatch, capsys):
    from foragent import orch_facts_ci

    case = facts_case(tmp_path, monkeypatch)
    record(case, {UNKNOWN_JOB})
    result = facts(case, capsys)
    [job] = [job for job in result["ci_jobs"] if job["job"] == UNKNOWN_JOB]
    assert job["rising"] and job["limit_minutes"] is None, f"Fixture: a rising job without a limit: {job}"
    output = facts(case, capsys, human=True)
    # No numeric budget: neither GitHub's default nor any number written as a limit.
    owned(output, UNKNOWN_JOB, "ci_job_rising", [UNKNOWN_JOB], [number(8.4), UNKNOWN],
          [number(orch_facts_ci.DEFAULT_LIMIT_MINUTES), re.compile(r"(limit|budget|of|/)\s*\d", re.I)])


def test_mixed_alarms_text_agrees_with_json(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    record(case, set(SERIES))
    result = facts(case, capsys)
    alarmed = {job["job"]: job for job in result["ci_jobs"] if job["near_limit"] or job["rising"]}
    assert set(alarmed) == set(EXPECTED), f"JSON alarms: {sorted(alarmed)}"
    for name, (latest, limit, kind) in EXPECTED.items():
        assert (alarmed[name]["latest_minutes"], alarmed[name]["limit_minutes"]) == (latest, limit), alarmed[name]
        assert alarmed[name]["near_limit"] is (kind == "ci_job_near_limit"), alarmed[name]
    output = facts(case, capsys, human=True)
    names = sorted(alarmed)
    for name, job in alarmed.items():
        kind = "ci_job_near_limit" if job["near_limit"] else "ci_job_rising"
        limit = UNKNOWN if job["limit_minutes"] is None else number(job["limit_minutes"])
        others = [number(other["latest_minutes"]) for key, other in alarmed.items() if key != name]
        owned(output, name, kind, names, [number(job["latest_minutes"]), limit], others)

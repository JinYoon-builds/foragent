"""Acceptance shard times stay fresh without a person (issue shard-times-writer).

The repository statement reads the checked-in scripts/shard_times.json. The
writer statements exercise the public merge-owner pass (`merge_owner.tick`),
FA's one main writer, on a real local origin with the offline commit/push
transport used by test_main_writes_one_commit. Assertions read only origin's
main history and contents, the main-write request queue and FA's log output.

JUnit results of a main acceptance run are the acceptance baseline reports FA
already keeps in its state store (`runs/issue-<n>-acceptance-baseline.xml`,
written by run_baseline from a checkout of the base branch); "latest" is the
newest such report.

Break-it: no writer (today) fails the rewrite checks; wrong or unsummed times
fail the value checks; a rewrite despite >90% coverage or without JUnit fails
the "unchanged" checks; a doubled rewrite fails the one-commit count; a silent
skip fails the log-line check.
"""

from dataclasses import replace
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import main_write_requests as writes, merge_owner
from foragent.scheduler import TickReport
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 7, 6, tzinfo=UTC)
TIMES = "scripts/shard_times.json"
REPO = Path(__file__).resolve().parents[2]
FILES = [f"tests/acceptance/test_area_{index}.py" for index in range(10)]
TICKS = 3


def acceptance_test_files(root):
    directory = root / "tests" / "acceptance"
    return sorted(path.relative_to(root).as_posix() for path in directory.rglob("test_*.py")
                  if "__pycache__" not in path.parts)


# Statement 1 -----------------------------------------------------------------

def test_main_times_at_least_ninety_percent_of_acceptance_files():
    files = acceptance_test_files(REPO)
    times = json.loads((REPO / TIMES).read_text(encoding="utf-8"))
    assert isinstance(times, dict)
    timed = [name for name in files
             if isinstance(times.get(name), (int, float)) and not isinstance(times.get(name), bool)]
    untimed = sorted(set(files) - set(timed))
    assert files, "no acceptance test files found"
    assert len(timed) >= 0.9 * len(files), (
        f"{TIMES} times {len(timed)} of {len(files)} acceptance files; untimed include {untimed[:10]}")


# Writer statements -----------------------------------------------------------

@pytest.fixture
def case(offline_publication, git_history, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root, config=replace(config, merge_owner=True),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin, calls=calls,
                           history=git_history, start=None)


def seed_main(case, timed):
    """Put FILES and a shard-times file timing `timed` of them on main; sync the checkout."""
    files = {ACCEPTANCE: None}
    files.update({name: "def test_it():\n    assert True\n" for name in FILES})
    old = {name: 1.0 for name in FILES[:timed]}
    files[TIMES] = json.dumps(old, indent=0, sort_keys=True) + "\n"
    case.history.update(files, message="Seed acceptance files")
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.start = git_command(case.origin, "rev-parse", "main")
    return files[TIMES]


def write_junit(case, n, seconds, *, age=0):
    """A legacy-family acceptance baseline JUnit report, as run_baseline keeps it."""
    cases = []
    for name, (first, second) in seconds.items():
        module = name.removesuffix(".py").replace("/", ".")
        cases.append(f'<testcase classname="{module}" name="test_one" file="{name}" line="1" time="{first}"/>')
        cases.append(f'<testcase classname="{module}" name="test_two" file="{name}" line="4" time="{second}"/>')
    total = sum(a + b for a, b in seconds.values())
    text = ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0" '
            f'failures="0" skipped="0" tests="{len(cases)}" time="{total}">' + "".join(cases)
            + "</testsuite></testsuites>\n")
    path = case.store.root / "runs" / f"issue-{n}-acceptance-baseline.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = NOW.timestamp() - age
    os.utime(path, (stamp, stamp))
    return path


def owner_passes(case, caplog):
    reports = []
    with caplog.at_level(logging.DEBUG):
        for _ in range(TICKS):
            report = TickReport()
            merge_owner.tick(case.ctx, report)
            reports.append(report)
    return reports


def main_times_text(case):
    return git_command(case.origin, "show", f"main:{TIMES}") + "\n"


def times_commits(case):
    out = git_command(case.origin, "log", "--format=%H", f"{case.start}..main", "--", TIMES)
    return [line for line in out.splitlines() if line]


def requests_touching_times(case):
    return [row for row in writes.requests(case.store)
            if TIMES in (row.get("payload") or {}).get("files", {})]


def log_text(case, reports, caplog):
    lines = [caplog.text]
    for report in reports:
        lines.extend(report.actions)
        if report.skipped:
            lines.append(report.skipped)
    for path in case.store.root.rglob("*"):
        if path.is_file() and path.suffix in {".log", ".jsonl", ".txt", ""}:
            lines.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(lines)


SECONDS = {name: (round(1.25 + index, 2), round(0.5 * index + 0.3, 2)) for index, name in enumerate(FILES)}


def expected(seconds):
    return {name: round(a + b, 1) for name, (a, b) in seconds.items()}


def test_rewrites_times_from_main_junit_when_too_many_untimed(case, caplog):
    seed_main(case, timed=5)  # 50% untimed
    write_junit(case, 7, SECONDS)

    owner_passes(case, caplog)

    times = json.loads(main_times_text(case))
    for name, seconds in expected(SECONDS).items():
        assert name in times, f"{name} untimed after the rewrite"
        assert abs(times[name] - seconds) <= 0.051, (name, times[name], seconds)
    assert len(times_commits(case)) == 1, "the rewrite must land on main exactly once"


def test_rewrite_uses_the_latest_main_junit(case, caplog):
    seed_main(case, timed=0)
    stale = {name: (9.0, 9.0) for name in FILES}
    write_junit(case, 3, stale, age=3600)
    write_junit(case, 8, SECONDS, age=60)

    owner_passes(case, caplog)

    times = json.loads(main_times_text(case))
    for name, seconds in expected(SECONDS).items():
        assert abs(times.get(name, -1) - seconds) <= 0.051, (name, times.get(name), seconds)
    assert len(times_commits(case)) == 1


def test_ten_percent_untimed_is_left_alone_and_more_is_refreshed(case, caplog):
    before = seed_main(case, timed=9)  # exactly 10% untimed: not more than 10%
    write_junit(case, 7, SECONDS, age=3600)

    owner_passes(case, caplog)

    assert main_times_text(case) == before, "rewrote although only 10% were untimed"
    assert times_commits(case) == []
    assert requests_touching_times(case) == []

    # Two more untimed acceptance files land on main: 9 of 12 timed, 25% untimed.
    extra = [f"tests/acceptance/test_area_{index}.py" for index in (10, 11)]
    case.history.update({name: "def test_it():\n    assert True\n" for name in extra},
                        message="Add acceptance files")
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    seconds = dict(SECONDS)
    seconds.update({name: (2.0, 1.5) for name in extra})
    write_junit(case, 9, seconds, age=0)

    owner_passes(case, caplog)

    times = json.loads(main_times_text(case))
    for name, value in expected(seconds).items():
        assert abs(times.get(name, -1) - value) <= 0.051, (name, times.get(name), value)
    assert len(times_commits(case)) == 1, "the refresh must land on main exactly once"


def test_without_junit_times_unchanged_and_reason_logged(case, caplog):
    before = seed_main(case, timed=5)
    assert not list(case.store.root.rglob("*.xml")), "this case has no JUnit results"

    reports = owner_passes(case, caplog)

    assert main_times_text(case) == before
    assert times_commits(case) == []
    assert requests_touching_times(case) == []
    text = log_text(case, reports, caplog)
    reasons = [line for line in text.splitlines()
               if "shard_times" in line and "junit" in line.lower()]
    assert reasons, "no log line explains why scripts/shard_times.json was not refreshed"

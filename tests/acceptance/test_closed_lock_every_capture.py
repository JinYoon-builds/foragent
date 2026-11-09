"""Acceptance tests for closed-lock-alarm-every-capture (2026-11-09, follow-up of #2925, r1324).

The alarm for a closed issue's locked test failing on main reads every recorded capture and CI
observation, in any arrival order, and judges each test by main's latest measurement of it. A
failure recorded while the issue was still open never counts; a request names the commit of the
latest (failing) measurement. A newer partial capture never hides an older capture's failure; a
skipped or xfailed baseline result is not a failure; a test that recovered and fails again raises a
new request even when the earlier records came from before the alarm read captures. See
closed_lock_every_capture_support for the recorded world. Every test here fails on main
7abb1cd7; regression controls that already pass live in test_closed_lock_every_capture_guard.py.

Driven only through `fa gate tick --no-start` (which reads the baseline cache), `fa orch inbox
--json`, and ``orch_facts_promises.observe`` - the call every CI acceptance comparison makes with
the reports it accepted (ci_mode.read).

Break-it: letting the capture file listed last outrank the latest measurement, naming the open-time
first red commit instead of the latest measured one, reading a partial capture's missing test as
passing, keeping only the newest reading,
counting a skipped or xfailed result as failing, letting a CI non-pass outrank the capture that
says skipped, ignoring an open report whose commit has no capture, or raising the re-failure
twice each fail a check below.
"""

from datetime import timedelta

import pytest

from closed_lock_every_capture_support import capture, close, new_commit, pre_upgrade_alarm, reopen
from closed_lock_red_support import (
    ALL_NODES, CLOSED_FILE, CLOSED_ISSUE, CLOSED_NODE, CLOSED_PASSING, OTHER_CLOSED_ISSUE,
    OTHER_CLOSED_NODE, gate_pass, measure, names, names_commit, names_issue, requests_naming, setup,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, tick_case  # noqa: F401


@pytest.fixture
def world(tick_case):  # noqa: F811
    case = tick_case
    case.first = setup(case)
    # Main was green for these tests when their issues closed.
    measure(case, case.first, [])
    return case


def assert_one_request(case, capsys, node, issue, sha):
    rows = requests_naming(case, capsys, node)
    assert len(rows) == 1, f"Expected exactly one orchestrator request naming {node}; got {len(rows)}: {rows}"
    row = rows[0]
    assert names_issue(row, issue), f"The request must name the test's closed issue #{issue}: {row}"
    assert names_commit(row, sha), f"The request must name the main commit of the latest failing measurement {sha}: {row}"
    assert row.get("status") != "done", "The orchestrator must still have the request to read"
    return row


def observe_ci(case, sha, collected, passing):
    """One CI acceptance comparison accepted reports whose base side measured main at ``sha``."""
    from foragent import orch_facts_promises
    from foragent.config import load_config
    from foragent.scheduler import TickContext
    from foragent.state import StateStore

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    ctx = TickContext(repo="owner/repo", repo_root=case.root, tracker=case.tracker, store=case.store,
                      config=load_config(case.root), git=case.git, now=lambda: case.clock[0],
                      launcher=lambda n: 7000 + n, runner_process_exists=lambda n: True,
                      kill_runner=lambda pid: None)
    reports = [{"report": {"base_sha": sha, "base": {
        "complete": True, "collected_ids": sorted(collected), "passing_ids": sorted(passing)}}}]
    orch_facts_promises.observe(ctx, reports)


# ---------------------------------------------------------------- statement 1


def assert_no_request(case, capsys, node=CLOSED_NODE, issue=CLOSED_ISSUE):
    for row in inbox(case, capsys):
        assert not names(row, node), f"Main's latest measurement passes {node}; nothing to report: {row}"
        assert not names_issue(row, issue), f"Closed issue #{issue} broke nothing on main: {row}"


@pytest.mark.parametrize("latest", ["capture", "ci"])
def test_failure_recorded_while_open_raises_nothing_when_main_latest_measurement_passes(
        world, capsys, latest):
    case = world
    reopen(case)
    gate_pass(case, capsys)
    sha = new_commit(case)
    # Measured while the issue was still open: red by design. This capture finishes late.
    red_while_open = case.clock[0] - timedelta(minutes=1)
    close(case)
    case.clock[0] += timedelta(minutes=10)
    if latest == "capture":
        # A later capture of the same commit, after the issue closed, passes. Its file happens to
        # be listed before the late red capture's file.
        capture(case, sha, created_at=case.clock[0] - timedelta(minutes=1), name="a-latest-green")
        capture(case, sha, failing=[CLOSED_NODE], created_at=red_while_open, name="z-open-red")
    else:
        # Main's latest CI base run passes; the red capture taken while open arrives afterwards.
        observe_ci(case, sha, ALL_NODES, ALL_NODES)
        capture(case, sha, failing=[CLOSED_NODE], created_at=red_while_open, name="z-open-red")
    gate_pass(case, capsys)
    assert_no_request(case, capsys)
    gate_pass(case, capsys)
    assert_no_request(case, capsys)


# ---------------------------------------------------------------- statement 2


def test_latest_failing_measurement_names_its_commit_not_the_red_from_while_open(world, capsys):
    case = world
    reopen(case)
    gate_pass(case, capsys)
    while_open = new_commit(case)
    capture(case, while_open, failing=[CLOSED_NODE])  # Red by design: the issue is open.
    gate_pass(case, capsys)
    assert requests_naming(case, capsys, CLOSED_NODE) == [], "An open issue's lock raises nothing"

    close(case)
    gate_pass(case, capsys)
    latest = new_commit(case)
    capture(case, latest, failing=[CLOSED_NODE])
    gate_pass(case, capsys)
    row = assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, latest)
    assert not names_commit(row, while_open), (
        f"A failure recorded while the issue was open never counts; name {latest[:12]}: {row}")

    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, latest)


def test_latest_measurement_failing_raises_one_request_whatever_file_is_listed_last(world, capsys):
    case = world
    sha = new_commit(case)
    # Two captures of the same main commit land before one gate pass: the earlier one passed, the
    # latest one fails. The earlier capture's file is listed last.
    capture(case, sha, created_at=case.clock[0] - timedelta(minutes=10), name="z-earlier-green")
    capture(case, sha, failing=[CLOSED_NODE], created_at=case.clock[0] - timedelta(minutes=1),
            name="a-latest-red")
    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, sha)

    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, sha)


# ---------------------------------------------------------------- statement 3


def test_newer_partial_capture_read_first_does_not_hide_older_capture_failure(world, capsys):
    case = world
    older = new_commit(case)
    newer = new_commit(case)
    older_time = case.clock[0] - timedelta(minutes=10)

    # The newer capture ran only part of the suite: the locked file's sibling, not the locked test.
    partial = [node for node in ALL_NODES if node != CLOSED_NODE]
    assert CLOSED_PASSING in partial and CLOSED_FILE in CLOSED_PASSING
    capture(case, newer, collected=partial, created_at=case.clock[0])
    gate_pass(case, capsys)
    assert requests_naming(case, capsys, CLOSED_NODE) == [], "The partial capture shows no failure"

    # The older, full capture arrives later and shows the locked test failing.
    capture(case, older, failing=[CLOSED_NODE], created_at=older_time)
    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, older)

    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, older)


def test_newer_partial_capture_does_not_close_the_report_of_an_older_failure(world, capsys):
    case = world
    older = new_commit(case)
    newer = new_commit(case)
    capture(case, older, failing=[CLOSED_NODE], created_at=case.clock[0] - timedelta(minutes=10))
    capture(case, newer, collected=[node for node in ALL_NODES if node != CLOSED_NODE],
            created_at=case.clock[0])
    gate_pass(case, capsys)
    assert_one_request(case, capsys, CLOSED_NODE, CLOSED_ISSUE, older)

    # A later full capture still failing is the same red run: no second request.
    later = new_commit(case)
    capture(case, later, failing=[CLOSED_NODE])
    gate_pass(case, capsys)
    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1 and rows[0].get("status") != "done", f"Still one open request: {rows}"


# ---------------------------------------------------------------- statement 4


@pytest.mark.parametrize("capture_first", [True, False], ids=["capture-then-ci", "ci-then-capture"])
def test_skipped_and_xfailed_baseline_results_raise_no_request_whatever_arrives_first(
        world, capsys, capture_first):
    case = world
    sha = new_commit(case)
    nonpassing = [CLOSED_NODE, OTHER_CLOSED_NODE]  # skipped, xfailed
    passing = [node for node in ALL_NODES if node not in nonpassing]

    def record_capture():
        capture(case, sha, unknown=nonpassing)

    def record_ci():
        # CI's base side lists collected and passing ids; it cannot tell a skip from a failure.
        observe_ci(case, sha, ALL_NODES, passing)

    for step in ((record_capture, record_ci) if capture_first else (record_ci, record_capture)):
        step()
        gate_pass(case, capsys)
    gate_pass(case, capsys)

    for row in inbox(case, capsys):
        for node, issue in ((CLOSED_NODE, CLOSED_ISSUE), (OTHER_CLOSED_NODE, OTHER_CLOSED_ISSUE)):
            assert not names(row, node), f"A skipped or xfailed baseline result is not a failure: {row}"
            assert not names_issue(row, issue), f"Closed issue #{issue} broke nothing: {row}"


# ---------------------------------------------------------------- statement 5


def test_recovery_then_new_failure_after_pre_upgrade_report_raises_a_new_request(world, capsys):
    case = world
    reported = new_commit(case)
    old = pre_upgrade_alarm(case, reported)
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 1

    recovered = new_commit(case)
    capture(case, recovered)  # The locked test passes on main again.
    red_again = new_commit(case)
    capture(case, red_again, failing=[CLOSED_NODE])
    gate_pass(case, capsys)

    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 2, f"Passing and failing anew is a new red run with one new request: {rows}"
    new = [row for row in rows if row.get("id") != old["id"]]
    assert len(new) == 1, rows
    assert names_issue(new[0], CLOSED_ISSUE), new[0]
    assert names_commit(new[0], red_again), f"The new request names the new first red commit: {new[0]}"
    assert not names_commit(new[0], reported), new[0]
    assert new[0].get("status") != "done"

    gate_pass(case, capsys)
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 2, "One request per red run"

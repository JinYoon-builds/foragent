"""Acceptance tests for stale-base-main-red (2026-11-09, PM r1433).

1. A failure measured on a base that is an older ancestor of a later main measurement in which
   that same test passes does not open a main-red entry; a later measurement that did not run the
   test changes nothing (#2951).
2. A main-red entry already open closes on the next gate pass after main's latest measurement
   shows the test passing.

Driven only through `fa gate tick --no-start` (reads main's recorded captures), `fa brief --report
--json` (``broken_promises``: the open main-red entries), `fa orch inbox --json`, and
``orch_facts_promises.observe`` - the call each CI acceptance comparison makes with the reports it
accepted. See stale_base_main_red_support for the recorded world. Every test here fails on main
bed981fe; regression controls that already pass (including #2951's "a later measurement that did
not run the test changes nothing" when the stale failure is the newest reading) live in
test_stale_base_main_red_guard.py.

Main already judges each test by its readings in main's commit order, so the shapes of statement 1
it handles (stale failure read after or with a later green measurement, recorded later in time,
reported by CI, or followed by a newer partial capture) are guards. The shape it misses: main's
later commit holds two measurements, the newer one green and an earlier red one whose capture is
read last; main lets arrival order pick the red one and opens an entry from the stale failure.

Break-it: letting arrival order instead of measurement time decide between two measurements of one
commit, opening an entry from the stale failure, keeping the entry when main's latest measurement
of the test passes, closing it twice, or reopening it on the next pass each fail a check below.
"""

from datetime import timedelta

import pytest

from closed_lock_every_capture_support import capture, new_commit
from closed_lock_red_support import ALL_NODES, CLOSED_NODE, gate_pass, measure, requests_naming, setup
from stale_base_main_red_support import (
    assert_no_entry, broken_on_main, capture_latest_green, capture_stale_failure, cleared_events, observe_ci,
    stale_world,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401

STALE = ("A failure measured on an older ancestor of a later main measurement that ran the test and "
         "saw it pass opens no entry")


@pytest.fixture
def world(tick_case):  # noqa: F811
    case = tick_case
    case.first = setup(case)
    # Main was green for these tests when their issues closed.
    measure(case, case.first, [])
    return stale_world(case)


# ---------------------------------------------------------------- statement 1


@pytest.mark.parametrize("arrival", ["red-read-in-same-pass", "red-read-in-later-pass"])
def test_stale_failure_opens_no_entry_when_main_latest_measurement_passes(world, capsys, arrival):
    """The stale base failed the lock four days ago. Main's later commit was measured twice: the
    newest measurement ran the lock and passed; an earlier one of the same commit failed it and
    its capture is read after the newer one. The later main measurement in which the test passes
    outranks the stale failure, so no entry opens, in any arrival order."""
    case = world
    capture_stale_failure(case)
    capture_latest_green(case, created_at=case.clock[0] - timedelta(minutes=1))
    earlier_red = lambda: capture(case, case.latest, failing=[CLOSED_NODE],  # noqa: E731
                                  created_at=case.clock[0] - timedelta(minutes=15), name="z-earlier-red")
    if arrival == "red-read-in-same-pass":
        earlier_red()
    else:
        gate_pass(case, capsys)
        assert_no_entry(case, capsys, why=STALE)
        earlier_red()
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)
    gate_pass(case, capsys)
    assert_no_entry(case, capsys, why=STALE)


# ---------------------------------------------------------------- statement 2


@pytest.mark.parametrize("latest", ["capture", "ci"])
def test_open_entry_closes_on_next_gate_pass_when_main_latest_measurement_passes(world, capsys, latest):
    case = world
    # The entry opened while the red commit was main's latest measured commit.
    red = case.latest
    capture(case, red, failing=[CLOSED_NODE], created_at=case.clock[0] - timedelta(minutes=1))
    gate_pass(case, capsys)
    assert CLOSED_NODE in broken_on_main(case, capsys), "The red lock on main's latest commit is open"
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 1

    # Main moves on. Its latest measurement passes the lock; a capture of the same commit taken
    # earlier, whose file happens to be read later, still shows it failing.
    newest = new_commit(case)
    earlier = case.clock[0] - timedelta(minutes=15)
    if latest == "capture":
        capture(case, newest, created_at=case.clock[0] - timedelta(minutes=1), name="a-latest-green")
    else:
        observe_ci(case, newest, ALL_NODES, ALL_NODES)
    capture(case, newest, failing=[CLOSED_NODE], created_at=earlier, name="z-earlier-red")
    gate_pass(case, capsys)

    broken = broken_on_main(case, capsys)
    assert CLOSED_NODE not in broken, (
        f"Main's latest measurement passes {CLOSED_NODE}; the entry closes on this gate pass: {broken}")
    assert len(cleared_events(case)) == 1, f"The entry closes once: {cleared_events(case)}"

    gate_pass(case, capsys)
    assert CLOSED_NODE not in broken_on_main(case, capsys), "A closed entry stays closed"
    assert len(cleared_events(case)) == 1, "The entry closes once"
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 1, "Closing raises no new request"

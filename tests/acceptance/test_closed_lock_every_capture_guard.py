"""Regression controls for closed-lock-alarm-every-capture (2026-11-09, r1324).

These already pass on main 7abb1cd7 and stay outside Done when; they keep the follow-up from
breaking what the alarm already does right. The checks that fail on main are in
test_closed_lock_every_capture.py.
"""

from closed_lock_every_capture_support import capture, close, new_commit, pre_upgrade_alarm, reopen
from closed_lock_red_support import (
    ALL_NODES, CLOSED_ISSUE, CLOSED_NODE, gate_pass, names, names_commit, names_issue, requests_naming,
)
from test_closed_lock_every_capture import observe_ci, world  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, tick_case  # noqa: F401


def test_red_while_open_then_green_on_a_later_commit_raises_nothing(world, capsys):  # noqa: F811
    case = world
    reopen(case)
    gate_pass(case, capsys)
    capture(case, new_commit(case), failing=[CLOSED_NODE])  # Red by design while open.
    gate_pass(case, capsys)
    close(case)
    capture(case, new_commit(case))
    gate_pass(case, capsys)
    gate_pass(case, capsys)
    for row in inbox(case, capsys):
        assert not names(row, CLOSED_NODE) and not names_issue(row, CLOSED_ISSUE), row


def test_skipped_capture_alone_raises_no_request(world, capsys):  # noqa: F811
    case = world
    capture(case, new_commit(case), unknown=[CLOSED_NODE])
    gate_pass(case, capsys)
    assert requests_naming(case, capsys, CLOSED_NODE) == []


def test_closed_lock_red_on_the_latest_commit_raises_one_request(world, capsys):  # noqa: F811
    case = world
    sha = new_commit(case)
    capture(case, sha, failing=[CLOSED_NODE])
    gate_pass(case, capsys)
    gate_pass(case, capsys)
    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1 and names_issue(rows[0], CLOSED_ISSUE) and names_commit(rows[0], sha), rows


def test_new_failure_after_pre_upgrade_report_and_recovery_read_in_turn_raises_a_new_request(
        world, capsys):  # noqa: F811
    case = world
    reported = new_commit(case)
    old = pre_upgrade_alarm(case, reported)
    first_pass = new_commit(case)
    # The recovery is read through CI, the failure through a capture, in one gate pass.
    observe_ci(case, first_pass, ALL_NODES, ALL_NODES)
    red_again = new_commit(case)
    capture(case, red_again, failing=[CLOSED_NODE])
    gate_pass(case, capsys)

    rows = [row for row in requests_naming(case, capsys, CLOSED_NODE) if row.get("id") != old["id"]]
    assert len(rows) == 1, f"The re-failure after the recovery raises one new request: {rows}"
    assert names_commit(rows[0], red_again), rows[0]

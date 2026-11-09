"""Acceptance tests for closed-lock-red-alarm (2026-11-09).

A locked acceptance test whose issue is closed must pass on main. When main's recorded acceptance
results show one failing, a gate pass (`fa gate tick --no-start`) puts one request in the
orchestrator inbox naming the test, its closed issue and the main commit first measured red; it
stays quiet while the same test stays red, and tells again after it passed and failed anew. Open
issues' locks failing raise nothing. See closed_lock_red_support for the recorded world.

Break-it: never raising, naming the newest red commit instead of the first one, naming the wrong
issue, raising again on every pass while the test stays red, raising again after the orchestrator
closed the request, never raising again after a pass and a new failure, or naming an open issue's
failing lock each fail a check below.
"""

import pytest

from closed_lock_red_support import (
    CLOSED_ISSUE, CLOSED_NODE, CLOSED_PASSING, OPEN_FILE, OPEN_ISSUE, OPEN_NODE, OTHER_CLOSED_ISSUE,
    OTHER_CLOSED_NODE, gate_pass, measure, names, names_commit, names_issue, red_main,
    requests_naming, setup,
)
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, tick_case  # noqa: F401


@pytest.fixture
def world(tick_case):  # noqa: F811
    case = tick_case
    case.first = setup(case)
    # Main was green for this test when its issue closed.
    measure(case, case.first, [])
    return case


def only_request(case, capsys, node):
    rows = requests_naming(case, capsys, node)
    assert len(rows) == 1, (f"Expected exactly one orchestrator request naming {node}; "
                            f"got {len(rows)}: {rows}")
    return rows[0]


def assert_names(row, node, issue, sha):
    assert names(row, node), f"The request must name the failing test {node}: {row}"
    assert names_issue(row, issue), f"The request must name the test's closed issue #{issue}: {row}"
    assert names_commit(row, sha), f"The request must name the main commit first measured red {sha}: {row}"


# ---------------------------------------------------------------- statement 1


def test_closed_lock_red_on_main_reaches_the_orchestrator_once_with_test_issue_and_first_red_commit(
        world, capsys):
    case = world
    gate_pass(case, capsys)
    assert requests_naming(case, capsys, CLOSED_NODE) == [], "Nothing is red yet"

    first_red = red_main(case, [CLOSED_NODE])
    red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names(row, CLOSED_NODE, CLOSED_ISSUE, first_red)
    assert row.get("status") != "done", "The orchestrator must still have the request to read"
    # A passing sibling in the same locked file is not named as failing.
    assert requests_naming(case, capsys, CLOSED_PASSING) == []


def test_each_closed_lock_names_its_own_issue_and_first_red_commit(world, capsys):
    case = world
    red_at_first = red_main(case, [CLOSED_NODE])
    red_at_second = red_main(case, [CLOSED_NODE, OTHER_CLOSED_NODE])
    gate_pass(case, capsys)

    first = only_request(case, capsys, CLOSED_NODE)
    assert_names(first, CLOSED_NODE, CLOSED_ISSUE, red_at_first)
    second = only_request(case, capsys, OTHER_CLOSED_NODE)
    assert_names(second, OTHER_CLOSED_NODE, OTHER_CLOSED_ISSUE, red_at_second)


def test_open_lock_failing_beside_a_closed_one_is_not_raised(world, capsys):
    case = world
    red = red_main(case, [CLOSED_NODE, OPEN_NODE])
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names(row, CLOSED_NODE, CLOSED_ISSUE, red)
    for row in inbox(case, capsys):
        assert not names(row, OPEN_NODE) and OPEN_FILE not in str(row), (
            f"An open issue's lock fails on main by design and raises nothing: {row}")
        assert not names_issue(row, OPEN_ISSUE), f"The open issue #{OPEN_ISSUE} is not named: {row}"


# ---------------------------------------------------------------- statement 2


def test_still_red_test_raises_no_second_request_on_later_passes(world, capsys):
    case = world
    first_red = red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)
    only_request(case, capsys, CLOSED_NODE)

    # Later main commits are measured red again, and the gate passes several times.
    for _ in range(2):
        red_main(case, [CLOSED_NODE])
        gate_pass(case, capsys)
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names(row, CLOSED_NODE, CLOSED_ISSUE, first_red)


def test_still_red_test_stays_quiet_after_the_orchestrator_closes_the_request(world, capsys):
    case = world
    red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)
    row = only_request(case, capsys, CLOSED_NODE)

    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "request-status", row["id"], "done", "--answer",
                                "Seen; the owning lane fixes it.", "--repo-root", str(case.root)])
    assert code == 0, capsys.readouterr().err

    red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)
    gate_pass(case, capsys)

    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 1, f"A closed request is no reason to tell again while the test stays red: {rows}"


def test_test_that_passed_and_failed_anew_raises_a_new_request_naming_the_new_red_commit(world, capsys):
    case = world
    first_red = red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)
    only_request(case, capsys, CLOSED_NODE)

    red_main(case, [])  # The test passes on main again.
    gate_pass(case, capsys)
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 1, "Passing again raises nothing"

    second_red = red_main(case, [CLOSED_NODE])
    red_main(case, [CLOSED_NODE])  # Still red at the next commit.
    gate_pass(case, capsys)

    rows = requests_naming(case, capsys, CLOSED_NODE)
    assert len(rows) == 2, f"A new failure after a pass is a new episode with one new request: {rows}"
    new = [row for row in rows if names_commit(row, second_red)]
    assert len(new) == 1, f"The new request names the commit first measured red this time: {rows}"
    assert_names(new[0], CLOSED_NODE, CLOSED_ISSUE, second_red)
    old = [row for row in rows if row is not new[0]][0]
    assert names_commit(old, first_red)
    assert not names_commit(new[0], first_red), "The new episode starts at its own first red commit"

    # And it stays one per episode afterwards.
    red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)
    assert len(requests_naming(case, capsys, CLOSED_NODE)) == 2

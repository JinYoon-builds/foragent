"""Acceptance tests for alarm-ignores-premerge-base (2026-11-09).

A failure of a closed issue's locked test measured on a main commit that does not contain the
issue's merge never counts: such readings raise no request, and a red run on main commits that do
contain the merge is reported naming the first of those commits, never the batch base measured
before the member landed. See closed_lock_premerge_support for how the merge lands on main.

Break-it: counting pre-merge readings (raising after the member lands, or naming the batch base as
first red), raising nothing for a red merge commit, naming a later commit instead of the first red
one containing the merge, raising twice for one red run, or moving the report back to the batch base
when its capture arrives late each fail a check below.
"""

import pytest

from closed_lock_every_capture_support import new_commit
from closed_lock_premerge_support import land_member
from closed_lock_red_support import (
    CLOSED_ISSUE, CLOSED_NODE, CLOSED_PASSING, gate_pass, measure, names_commit, red_main,
    requests_naming, setup,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import tick_case  # noqa: F401

LOCKED = (CLOSED_NODE, CLOSED_PASSING)


@pytest.fixture
def world(tick_case):  # noqa: F811
    """The closed issue's locked tests are on main; its work is not merged yet, so main fails them."""
    case = tick_case
    case.first = setup(case)
    measure(case, case.first, list(LOCKED))
    return case


def only_request(case, capsys, node):
    rows = requests_naming(case, capsys, node)
    assert len(rows) == 1, (f"Expected exactly one orchestrator request naming {node}; "
                            f"got {len(rows)}: {rows}")
    return rows[0]


def assert_no_requests(case, capsys):
    for node in LOCKED:
        rows = requests_naming(case, capsys, node)
        assert rows == [], (f"{node} failed only on main commits without #{CLOSED_ISSUE}'s merge; "
                            f"that raises no request: {rows}")


def assert_names_only(row, sha, *not_shas):
    assert names_commit(row, sha), f"The request must name the main commit {sha} with the merge: {row}"
    for other in not_shas:
        assert not names_commit(row, other), (
            f"{other} does not contain #{CLOSED_ISSUE}'s merge and is never the first red commit: {row}")


# ---------------------------------------------------------------- statement 1


@pytest.mark.parametrize("capture", ["before-landing", "after-landing"])
def test_batch_base_red_before_the_member_landed_raises_no_request(world, capsys, capture):
    case = world
    if capture == "before-landing":
        base = red_main(case, list(LOCKED))
        merge = land_member(case)
    else:
        # The batch base's capture finishes only after the member merged and the issue closed.
        base = new_commit(case)
        merge = land_member(case)
        measure(case, base, list(LOCKED))
    assert base != merge
    gate_pass(case, capsys)
    gate_pass(case, capsys)
    assert_no_requests(case, capsys)


def test_premerge_red_stays_quiet_while_later_main_commits_are_unmeasured(world, capsys):
    case = world
    red_main(case, list(LOCKED))
    red_main(case, list(LOCKED))
    land_member(case)
    gate_pass(case, capsys)
    red_main(case, [])  # A later main commit, measured green.
    gate_pass(case, capsys)
    assert_no_requests(case, capsys)


# ---------------------------------------------------------------- statement 2


def test_red_on_the_merge_commit_raises_one_request_naming_it_not_the_batch_base(world, capsys):
    case = world
    base = red_main(case, list(LOCKED))
    merge = land_member(case)
    measure(case, merge, [CLOSED_NODE])
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names_only(row, merge, base, case.first)
    # The sibling was red only before the merge and passes on the merge commit.
    assert requests_naming(case, capsys, CLOSED_PASSING) == []


def test_red_first_measured_after_the_merge_names_that_later_commit(world, capsys):
    case = world
    base = red_main(case, list(LOCKED))
    merge = land_member(case)  # Never measured.
    later = red_main(case, [CLOSED_NODE])
    newest = red_main(case, [CLOSED_NODE])
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names_only(row, later, base, case.first, newest)
    assert merge != later


def test_red_after_the_merge_stays_one_request_naming_the_merge_commit(world, capsys):
    case = world
    base = red_main(case, list(LOCKED))
    merge = land_member(case)
    measure(case, merge, [CLOSED_NODE])
    gate_pass(case, capsys)
    for _ in range(2):
        red_main(case, [CLOSED_NODE])
        gate_pass(case, capsys)
    gate_pass(case, capsys)

    row = only_request(case, capsys, CLOSED_NODE)
    assert_names_only(row, merge, base, case.first)


def test_late_capture_of_the_batch_base_does_not_move_the_report_before_the_merge(world, capsys):
    case = world
    base = new_commit(case)  # The batch base, its capture still running.
    merge = land_member(case)
    measure(case, merge, [CLOSED_NODE])
    gate_pass(case, capsys)
    row = only_request(case, capsys, CLOSED_NODE)
    assert_names_only(row, merge, case.first)

    # The batch base's capture finishes later and shows the test red there too.
    measure(case, base, list(LOCKED))
    gate_pass(case, capsys)
    row = only_request(case, capsys, CLOSED_NODE)
    assert_names_only(row, merge, base, case.first)
    assert requests_naming(case, capsys, CLOSED_PASSING) == []

"""Acceptance tests for covered-skips-writer (PM q82).

Issue #7 is already published on the statement route with one statement and a locked acceptance
file on main (the world of `test_prepare_counts_existing_locks`). An order edits only its Goal:
the statement is unchanged, the published lock command is kept and no lock correction is carried,
so every statement is covered by the existing lock. Preparation must not start a writer or
reviewer for it, must record it ready to publish with its existing Done when, and
`fa order publish --approve` must then publish the edit.

Everything runs through `fa order prepare` and `fa order publish` on the offline toy project of
`lane_tests_case`; writer and reviewer are scripted executors that record each start.
"""

from test_prepare_counts_existing_locks import (  # noqa: F401
    GOAL, LOCKED, LOCKED_CHECK, LOCKED_TEST, NUMBER, OLD_STATEMENT, SCOPE, _edit, _origin_main_file,
    _plan_edit, _prepare, _roles, world)
from lane_tests_case import STATEMENT, lane_case, offline_publication, publish  # noqa: F401

NARROWED_GOAL = "A sent notice reports what it delivered, counted once per send."


def _goal_only(case):
    # As in #2560, a writer that is started anyway correctly writes nothing for covered statements.
    case.writer_files = {}
    case.writer_reply_files = []
    case.writer_done_when = []
    return _edit(case, goal=NARROWED_GOAL)


def test_a_goal_only_edit_starts_no_writer_and_is_ready_to_publish(world, capsys):
    case = world
    _goal_only(case)
    row = _prepare(case, capsys)
    assert _roles(case) == [], f"No test writer or reviewer starts for a covered-only edit: {case.calls}"
    assert row["status"] == "reviewed", f"The edit is recorded ready to publish: {row}"
    edit = _plan_edit(case)
    assert edit["done_when"] == [LOCKED_CHECK], f"Its existing Done when is kept as is: {edit}"
    assert not edit.get("acceptance_files"), f"No new test file is added: {edit}"
    assert _origin_main_file(case, LOCKED) == LOCKED_TEST
    assert case.tracker.issues[NUMBER].body == case.body, "Preparation publishes nothing"


def test_a_goal_only_edit_then_publishes_with_approve(world, capsys):
    case = world
    _goal_only(case)
    _prepare(case, capsys)
    assert _roles(case) == [], case.calls
    code, out, err = publish(case, capsys, "--approve")
    assert code == 0, out + err
    assert _roles(case) == [], "Publication starts no writer or reviewer either"
    body = case.tracker.issues[NUMBER].body
    assert NARROWED_GOAL in body, body
    assert GOAL + "\n" not in body.replace(NARROWED_GOAL, ""), "The old Goal is replaced"
    assert "## Behaviour\n- " + OLD_STATEMENT in body, body
    assert body.count(LOCKED_CHECK) >= 1, body
    assert _origin_main_file(case, LOCKED) == LOCKED_TEST, "The existing lock is not rewritten"
    labels = case.tracker.issues[NUMBER].labels
    assert "approved" in labels, labels


def test_preparing_twice_still_starts_no_writer(world, capsys):
    """A doubled preparation of the same covered-only edit stays without any agent."""
    case = world
    _goal_only(case)
    first = _prepare(case, capsys)
    second = _prepare(case, capsys)
    assert _roles(case) == [], case.calls
    assert first["status"] == second["status"] == "reviewed", (first, second)

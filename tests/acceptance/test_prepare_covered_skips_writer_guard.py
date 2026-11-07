"""Regression guards for covered-skips-writer (PM q82), outside Done when.

These controls already pass on main and must keep passing after the change: an edit with an
uncovered statement, or one that drops a published lock command, is still prepared with a
writer, and a covered-only edit still cannot publish without preparation.
"""

import json

from test_prepare_counts_existing_locks import (  # noqa: F401
    NUMBER, _edit, _prepare, _roles, world)
from test_prepare_counts_existing_locks import OLD_STATEMENT
from lane_tests_case import STATEMENT, lane_case, offline_publication, publish  # noqa: F401
from test_prepare_covered_skips_writer import NARROWED_GOAL, _goal_only


def test_an_edit_with_an_uncovered_statement_still_starts_the_writer(world, capsys):
    """Control: one statement the published issue never had is prepared as today."""
    case = world
    _edit(case, goal=NARROWED_GOAL, statements=[OLD_STATEMENT, STATEMENT])
    row = _prepare(case, capsys)
    assert _roles(case) == ["writer", "reviewer"], case.calls
    assert row["status"] == "reviewed", row


def test_a_goal_only_edit_that_drops_the_lock_command_is_not_skipped(world, capsys):
    """Control: without every published lock command the statement is not covered; a writer runs."""
    case = world
    _edit(case, goal=NARROWED_GOAL, done_when=["true"])
    _prepare(case, capsys)
    assert "writer" in _roles(case), case.calls


def test_a_goal_only_edit_is_not_published_without_preparation(world, capsys):
    """Control: the skip is a preparation outcome; publishing before preparing still refuses."""
    case = world
    _goal_only(case)
    code, out, err = publish(case, capsys, "--approve")
    assert code != 0, out + err
    assert case.tracker.issues[NUMBER].body == case.body
    assert json.loads(case.path.read_text())["edits"][0]["goal"] == NARROWED_GOAL

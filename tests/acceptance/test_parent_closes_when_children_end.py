"""Acceptance for parent-closes-when-children-end. Locked; do not edit.

Drive the public gate tick and read the tracker and board, using existing fakes.
Each test names the wrong implementation it rejects.
"""

from dataclasses import replace
import re

import pytest

from foragent.state import StateStore
from test_parent_follows_children import (
    BACKLOG, DONE, IN_PROGRESS, add_parent, card_column, gate_tick,
    offline_only, parent_case, seed_card,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401


def assert_closed_with_summary(case, children, cancelled=()):
    assert case.tracker.get_issue(10).state == "closed", "An order with no open child must close"
    assert card_column(case.tracker, case.board_id, 10) == DONE, "The order card must be Done"
    comments = case.tracker.list_comments(10)
    assert len(comments) == 1, "Closing an order must leave exactly one summary comment"
    # Lists may use headings, separate lines or prose. Wording and punctuation are free.
    groups = re.split(r"[\n.;]", comments[0].body.casefold())
    named = {}
    category = None
    for group in groups:
        for token in re.finditer(
            r"(?P<cancelled>cancelled|canceled|not[ _-]planned|취소|계획\s*안\s*함)"
            r"|(?P<completed>completed|finished|완료)|(?:#|/issues/)(?P<number>\d+)\b", group,
        ):
            if token.lastgroup == "number":
                named[int(token.group("number"))] = category
            else:
                category = token.lastgroup
    for number in children:
        assert named.get(number) == ("cancelled" if number in cancelled else "completed"), (
            f"The closing comment must name #{number} in the correct completed/cancelled group: "
            f"{comments[0].body}"
        )
    return comments


def test_all_completed_children_close_and_are_named(parent_case):
    """Reject moving only the card or summarising only a count without naming the children."""
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    children = add_parent(case, ["completed", "completed"])
    seed_card(case.tracker, case.board_id, 10, IN_PROGRESS)
    gate_tick(case)
    assert_closed_with_summary(case, children)


@pytest.mark.parametrize("states", [
    pytest.param(["completed", "not_planned"], id="completed-and-cancelled"),
    pytest.param(["not_planned", "not_planned"], id="all-cancelled"),
])
def test_cancelled_children_count_as_ended_and_are_named(parent_case, states):
    """Reject requiring every child to be completed or reporting cancellations as completions."""
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    children = add_parent(case, states)
    cancelled = [number for number, state in zip(children, states, strict=True)
                 if state == "not_planned"]
    # This is the observable result of order-replaces; its implementation is not a dependency.
    case.tracker.comment(cancelled[0], "Replaced by #900.")
    seed_card(case.tracker, case.board_id, 10, BACKLOG)
    gate_tick(case)
    assert_closed_with_summary(case, children, cancelled)


def test_one_open_child_keeps_parent_open_until_that_child_ends(parent_case):
    """Reject closing an order just because some children ended, then lock its last-child transition."""
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    children = add_parent(case, ["completed", "unapproved"])
    gate_tick(case)
    assert case.tracker.get_issue(10).state == "open"
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS
    assert case.tracker.list_comments(10) == []

    child = children[-1]
    case.tracker.issues[child] = replace(case.tracker.get_issue(child),
                                        state="closed", state_reason="not_planned")
    gate_tick(case, refresh=True)
    assert_closed_with_summary(case, children, [child])


def test_next_tick_does_not_repeat_the_close_or_summary(parent_case):
    """Reject repeating closure or its comment after a tick or a gate-state reload."""
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    children = add_parent(case, ["completed", "not_planned"])
    gate_tick(case)
    comments = assert_closed_with_summary(case, children, [children[-1]])
    closures = list(case.tracker.closures)

    gate_tick(case)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    gate_tick(case, refresh=True)
    assert case.tracker.closures == closures, "An already-closed parent must not close again"
    assert case.tracker.list_comments(10) == comments, "A subsequent tick must not add a second comment"
    assert card_column(case.tracker, case.board_id, 10) == DONE

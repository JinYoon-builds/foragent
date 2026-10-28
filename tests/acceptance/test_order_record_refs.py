"""Acceptance test for order-record-refs. Locked by Foragent; do not edit."""

import json

from foragent import board
from foragent.state import StateStore
from test_order_record_close import assert_closed_once
from test_parent_follows_children import gate_tick, offline_only, parent_case  # noqa: F401 (fixtures)
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)

# Evidence (2026-10-27): 24 old order records stayed open in Backlog. They were edit-only
# (no GitHub sub-issues), or older than native sub-issue edges, so the tick's catch-up,
# which reads only sub-issues, never closed them. A record's work is every issue its plan
# marker references: the map values plus the issue numbers in its acceptance (edit) map.


def add_record(case, number, *, mapping=None, edited=()):
    acceptance = {str(n): {"tests/acceptance/test_x.py": "0" * 40} for n in edited}
    marker = (f"<!-- foragent:plan id=record-{number} map={json.dumps(mapping or {})} "
              f"acceptance={json.dumps(acceptance)} -->")
    case.tracker.add_issue(number, f"Order {number}", "Answers a question.\n\n" + marker)
    board.add_issue(case.tracker, "owner/repo", case.board_id, number)


def add_work(case, number, *, closed=False):
    case.tracker.add_issue(number, f"Work {number}", case.work_body,
                           labels=[] if closed else ["approved"],
                           state="closed" if closed else "open",
                           state_reason="completed" if closed else None)


def tick_again(case):
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    gate_tick(case, refresh=True)


def test_an_edit_only_record_closes_once_its_edited_issue_closes(parent_case):  # noqa: F811
    case = parent_case
    add_work(case, 41)
    add_record(case, 40, edited=[41])
    gate_tick(case)
    assert case.tracker.get_issue(40).state == "open", "A record that edits open work stays open"
    assert case.tracker.closures == []
    case.tracker.close_issue(41)
    tick_again(case)
    assert case.tracker.get_issue(40).state == "closed", "Close the record once its edited issue is closed"
    assert_closed_once(case, 40)


def test_a_record_without_sub_issue_edges_follows_its_map(parent_case):  # noqa: F811
    case = parent_case
    add_work(case, 51, closed=True)
    add_work(case, 52)
    add_record(case, 50, mapping={"a": 51, "b": 52})
    gate_tick(case)
    assert case.tracker.get_issue(50).state == "open", "Any referenced open issue keeps the record open"
    case.tracker.close_issue(52)
    tick_again(case)
    assert case.tracker.get_issue(50).state == "closed"
    assert_closed_once(case, 50)


"""#1577: a parent that already closed still finishes its card in Done.

The locked parent-sync harness supplies real ticks with four-second calls.
Its fixtures are autouse, so these tests live apart from test_board_parents.py.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_parent_follows_children import (  # noqa: E402
    DONE, add_parent, card_column, gate_tick, offline_only, parent_case,  # noqa: F401
)
from test_parent_sync_starve import SequenceLatency, finish_sequence, sequence_case  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401


@pytest.mark.parametrize(("states", "references", "race", "sequence"), [
    pytest.param(("completed",), 1, False,
                 ("children", "reference", "close", "board", "card", "add-card", "status"),
                 id="native-reference-close-add-card-done"),
    pytest.param((), 3, False,
                 ("reference",) * 3 + ("close", "board", "card", "add-card", "status"),
                 id="reference-only-close-add-card-done"),
    pytest.param(("completed", "completed"), 1, True,
                 ("children", "reference", "close", "board", "card", "add-card", "status"),
                 id="sub-issues-fallback-close-add-card-done"),
])
def test_closed_parent_without_a_card_gets_one_in_done(parent_case, monkeypatch,  # noqa: F811
                                                       states, references, race, sequence):
    case = parent_case
    children, extra = sequence_case(case, monkeypatch, states, references, "missing-card", race=race)
    latency = SequenceLatency(case.tracker, monkeypatch)
    finish_sequence(case, latency, sequence, closes=True, column=DONE)
    # Finished: later ticks with an unchanged listing spend no parent calls.
    for _ in range(2):
        latency.gate_tick(case)
        assert not [call for call in latency.calls if call[2]], latency.calls
    assert card_column(case.tracker, case.board_id, 10) == DONE


def test_older_closed_parent_without_a_card_gets_one_in_done(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    # Closed earlier by a person or an older gate, with no card left on the board.
    for number in [*children, 10]:
        case.tracker.issues[number] = replace(case.tracker.issues[number], state="closed",
                                              state_reason="completed")
    project = case.tracker._projects.projects[case.board_id]
    project["items"] = {key: item for key, item in project["items"].items()
                        if item["contentId"] != "I_owner/repo#10"}
    latency = SequenceLatency(case.tracker, monkeypatch)
    for _ in range(8):
        latency.gate_tick(case)
        try:
            if card_column(case.tracker, case.board_id, 10) == DONE:
                break
        except StopIteration:
            pass
    assert card_column(case.tracker, case.board_id, 10) == DONE
    assert not case.tracker.comments.get(10), "An already closed parent gets no new closing comment"


def native_parent(case, monkeypatch, *, closed_children):
    """A person's parent with native sub-issues only: no plan marker, so only the listing summary remains once closed."""
    from foragent import board

    case.config = case.ctx.config = replace(case.config, language="en")
    case.tracker.add_issue(10, "Order 10", "Ship the order.")
    board.add_issue(case.tracker, "owner/repo", case.board_id, 10)
    children = [101, 102]
    for child in children:
        case.tracker.add_issue(child, f"Child {child}", case.work_body)
        case.tracker.add_sub_issue(10, child)
    listing = case.tracker.list_issues

    def listed(state="all"):
        issues = []
        for issue in listing(state):
            if issue.number == 10:
                ended = sum(case.tracker.issues[n].state == "closed" for n in children)
                issue = replace(issue, sub_issues_summary=(
                    ("total", len(children)), ("completed", ended), ("percent_completed", 50 * ended)))
            issues.append(issue)
        return issues

    monkeypatch.setattr(case.tracker, "list_issues", listed)
    gate_tick(case)
    gate_tick(case, refresh=True)
    if closed_children:
        for child in children:
            case.tracker.issues[child] = replace(case.tracker.issues[child], state="closed",
                                                 state_reason="completed")
    project = case.tracker._projects.projects[case.board_id]
    project["items"] = {key: item for key, item in project["items"].items()
                        if item["contentId"] != "I_owner/repo#10"}
    return children


def finish_in_done(case, latency, ticks=8):
    for _ in range(ticks):
        assert latency.gate_tick(case).skipped is None
        try:
            if card_column(case.tracker, case.board_id, 10) == DONE:
                return
        except StopIteration:
            pass
    pytest.fail(f"The closed parent's card did not reach Done in {ticks} ticks: {latency.calls}")


def test_newly_closed_native_parent_without_a_marker_gets_its_card_in_done(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = native_parent(case, monkeypatch, closed_children=True)
    latency = SequenceLatency(case.tracker, monkeypatch)
    finish_in_done(case, latency)
    assert case.tracker.issues[10].state == "closed"
    assert [n for n, _ in case.tracker.closures if n == 10] == [10]
    assert all(f"#{n}" in case.tracker.comments[10][0].body for n in children)
    row = next(row for row in case.store.local_issues() if row["number"] == 10)
    assert not row.get("children"), "The refresh after the close must have cleared the closed row's edges"
    for _ in range(2):
        latency.gate_tick(case)
        assert not [call for call in latency.calls if call[2]], latency.calls


def test_older_closed_native_parent_without_a_marker_gets_its_card_in_done(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    native_parent(case, monkeypatch, closed_children=True)
    case.tracker.issues[10] = replace(case.tracker.issues[10], state="closed", state_reason="completed")
    latency = SequenceLatency(case.tracker, monkeypatch)
    finish_in_done(case, latency)
    assert not case.tracker.comments.get(10), "An already closed parent gets no new closing comment"

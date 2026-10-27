"""Acceptance test for order-record-close. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from foragent import board, order
from foragent.issue import parse_body
from foragent.state import StateStore
from test_board_projects import board_snapshot
from test_board_status_groups import seed_card
from test_order import publishing
from test_parent_follows_children import (
    BACKLOG, DONE, ClosureTracker, add_parent, card_column, gate_tick,
    offline_only, parent_case,
)
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit, isolated_home


EDIT = {
    "number": 3, "goal": "Apply the decided change.", "out_of_scope": "Other work.",
    "done_when": ["true"], "checklist": [], "acceptance_files": {},
}


@pytest.fixture
def publication(publishing, monkeypatch):
    root, config, _, git, events = publishing
    tracker = ClosureTracker(actor="human")
    board_id = board.ensure(tracker, "owner/repo")
    assert board_id, "Publication must use a configured offline board"
    config = replace(config, board=board_id, language="en", skill_libraries=[])
    main = with_clean_preread(
        publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch,
    )
    return SimpleNamespace(root=root, config=config, tracker=tracker, board_id=board_id,
                           main=main, events=events)


def publish(case, capsys, *, issues=(), edits=(), approve=True):
    plan = {
        "id": "order-record-close", "title": "Apply the order", "summary": "Apply the decision.",
        "spec_markdown": "# Apply the decision\n", "issues": list(issues), "edits": list(edits),
    }
    path = case.root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    argv = ["order", "publish", str(path), "--repo-root", str(case.root)]
    if approve:
        argv.append("--approve")
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    assert code == 0, f"fa order publish must succeed: {output.out}{output.err}"
    records = [issue for issue in case.tracker.list_issues("all")
               if "<!-- foragent:plan " in issue.body]
    assert len(records) == 1, "Publication must create exactly one order record"
    record = records[0]
    assert not any(label.startswith("lane:") for label in record.labels)
    return record


def new_work():
    return {
        "key": "work", "title": "Implement the decision", "goal": "Implement the feature.",
        "out_of_scope": "Other work.", "done_when": ["true"], "lane": "main", "size": "s",
        "blocked_by": [], "acceptance_files": {},
    }


def assert_closed_once(case, number):
    issue = case.tracker.get_issue(number)
    assert issue.state_reason == "completed", "Order records must close as completed"
    comments = case.tracker.list_comments(number)
    assert len(comments) == 1, "Closing an old order record must leave exactly one summary comment"
    assert card_column(case.tracker, case.board_id, number) == DONE
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(2):
        gate_tick(case, refresh=True)
    assert [entry for entry in case.tracker.closures if entry[0] == number] == [
        (number, "completed"),
    ], "Repeated ticks after reloading state must close an order record only once"
    assert case.tracker.list_comments(number) == comments, "Repeated ticks must not repeat the summary"
    assert card_column(case.tracker, case.board_id, number) == DONE


@pytest.mark.parametrize("kind", ["edit", "approval"])
def test_publish_edit_only_closes_record_immediately(publication, capsys, kind):
    case = publication
    body = ("The previous decision." if kind == "edit" else
            order.issue_body(EDIT, "owner/repo", case.config.base_branch))
    case.tracker.add_issue(3, "Keep this title", body, labels=["size:l", "custom"])
    record = publish(case, capsys, edits=[EDIT])

    edited = case.tracker.get_issue(3)
    assert edited.state == "open", "Closing the order record must not close the edited work"
    assert edited.state_reason is None
    assert edited.title == "Keep this title"
    assert edited.labels == {"size:l", "custom", "approved"}, "Normal edit approval must be preserved"
    assert parse_body(edited.body).done_when_commands == EDIT["done_when"]
    assert EDIT["goal"] in edited.body and EDIT["out_of_scope"] in edited.body
    assert case.tracker.parent_of(3) is None, "An edit must not become a new sub-issue"
    assert case.tracker.sub_issues(record.number) == []
    assert record.state == "closed", "An edit-only order record must be closed when publish returns"
    assert record.state_reason == "completed", "An applied order record must close as completed"
    comments = case.tracker.list_comments(record.number)
    assert len(comments) == 1, "An applied order record must receive exactly one completion comment"
    message = comments[0].body.casefold()
    assert "order" in message and "applied" in message, "The completion comment must say the order was applied"
    assert case.tracker.closures == [(record.number, "completed")]


@pytest.mark.parametrize("with_edits", [False, True], ids=["new-issues", "new-issues-and-edits"])
def test_publish_with_new_issues_keeps_record_open(publication, capsys, with_edits):
    case = publication
    if with_edits:
        case.tracker.add_issue(3, "Existing work", "Previous decision", labels=["size:l"])
    record = publish(case, capsys, issues=[new_work()], edits=[EDIT] if with_edits else [])
    assert record.state == "open", "An order that creates work must remain open for #411 to finish later"
    assert record.state_reason is None
    children = case.tracker.sub_issues(record.number)
    assert len(children) == 1 and children[0].state == "open"
    assert "approved" in children[0].labels
    assert case.tracker.parent_of(children[0].number) == record.number
    assert case.tracker.list_comments(record.number) == []
    assert case.tracker.closures == []


def test_tick_closes_old_record_with_completed_children_once(parent_case):
    case = parent_case
    children = add_parent(case, ["completed", "completed"])
    gate_tick(case)
    assert case.tracker.get_issue(10).state == "closed", (
        "The next gate tick must close an old order record after all sub-issues complete"
    )
    assert all(case.tracker.get_issue(child).state == "closed" for child in children)
    assert_closed_once(case, 10)


@pytest.mark.parametrize("states", [
    pytest.param(["unapproved"], id="all-open"),
    pytest.param(["completed", "unapproved"], id="one-open-sibling"),
])
def test_tick_keeps_record_with_open_sub_issue_open(parent_case, states):
    case = parent_case
    children = add_parent(case, states)
    before = [case.tracker.get_issue(child) for child in children]
    gate_tick(case)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    gate_tick(case, refresh=True)
    assert case.tracker.get_issue(10).state == "open", "Any open sub-issue must keep its order record open"
    assert case.tracker.closures == []
    assert case.tracker.list_comments(10) == []
    assert [case.tracker.get_issue(child) for child in children] == before


def test_tick_keeps_childless_non_records_open(parent_case):
    case = parent_case
    work = case.tracker.add_issue(20, "Unapproved work", "A task without an order marker.")
    assert "<!-- foragent:plan" not in work.body
    assert case.tracker.sub_issues(20) == []
    board.add_issue(case.tracker, "owner/repo", case.board_id, 20)
    add_parent(case, [], number=30)
    case.tracker.add_labels(30, ["lane:main"])
    assert "<!-- foragent:plan " in case.tracker.get_issue(30).body
    gate_tick(case)
    for number in (20, 30):
        assert case.tracker.get_issue(number).state == "open", (
            "A childless issue needs both the plan marker and no lane label to be an order record"
        )
        assert case.tracker.list_comments(number) == []
    assert case.tracker.closures == []


def test_publish_does_not_leave_order_record_in_backlog(publication, capsys):
    case = publication
    case.tracker.add_issue(3, "Existing work", "Previous decision", labels=["size:l"])
    record = publish(case, capsys, edits=[EDIT])
    assert "<!-- foragent:plan " in record.body
    assert case.tracker.sub_issues(record.number) == []
    items = [item for item in board_snapshot(case.tracker, case.board_id)["items"]["nodes"]
             if item["content"]["number"] == record.number]
    assert not [item for item in items for value in item["fieldValues"]["nodes"]
                if value["field"]["name"] == "Status" and value["name"] == BACKLOG], (
        "Publishing must not leave an order record in Backlog"
    )
    assert record.state == "closed"


def test_next_tick_moves_externally_closed_card_to_done_once(parent_case, monkeypatch):
    from datetime import timedelta

    from test_board_status_groups import column_updates, record_board_gh

    case = parent_case
    case.tracker.add_issue(40, "Work closed outside the gate", case.work_body)
    seed_card(case.tracker, case.board_id, 40, BACKLOG)
    case.tracker.add_issue(50, "Work already done", case.work_body,
                           state="closed", state_reason="completed")
    seed_card(case.tracker, case.board_id, 50, DONE)
    gate_tick(case)  # Establish the ordinary cached listing before the external close.
    item_ids = {item["content"]["number"]: item["id"]
                for item in board_snapshot(case.tracker, case.board_id)["items"]["nodes"]}
    calls = record_board_gh(case.tracker, monkeypatch)
    case.clock[0] += timedelta(seconds=1)
    # Use the remote fake directly, just as a plain close outside the gate would.
    case.tracker.close_issue(40)
    assert case.tracker.get_issue(40).state == "closed"
    assert card_column(case.tracker, case.board_id, 40) == BACKLOG
    closures = list(case.tracker.closures)
    calls.clear()

    gate_tick(case)  # The very next tick, without waiting for a periodic refresh.
    assert card_column(case.tracker, case.board_id, 40) == DONE, (
        "The next gate tick must move an externally closed issue's existing card from Backlog to Done"
    )
    writes = column_updates(calls)
    assert [call["variables"]["input"]["itemId"] for call in writes] == [item_ids[40]], (
        "Board sync must move the closed card exactly once and leave the already-Done card untouched"
    )
    assert len([call for call in calls if call["query"].lstrip().startswith("mutation")]) == 1, (
        "Moving an existing closed card must not add another card or make other board mutations"
    )
    assert card_column(case.tracker, case.board_id, 50) == DONE

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for refresh in (False, True, False):
        calls.clear()
        gate_tick(case, refresh=refresh)
        assert not [call for call in calls if call["query"].lstrip().startswith("mutation")], (
            "Cards already in Done must receive no further board mutations, including after refresh"
        )
        assert card_column(case.tracker, case.board_id, 40) == DONE
        assert card_column(case.tracker, case.board_id, 50) == DONE
    assert case.tracker.closures == closures, "Board catch-up must not close an already-closed issue again"
    assert case.tracker.list_comments(40) == case.tracker.list_comments(50) == []

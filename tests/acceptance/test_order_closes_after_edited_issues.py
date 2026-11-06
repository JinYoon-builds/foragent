"""PM q34 (2026-11-06): orders finish only after their edited work finishes."""

from dataclasses import replace
import re

import pytest

from foragent.orch_events import events
from test_order_record_close import EDIT, new_work, publish
from test_parent_follows_children import (
    gate_tick, isolated_home, offline_only, parent_case,  # noqa: F401
)
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread


@pytest.fixture
def publication(parent_case, monkeypatch):
    """Publish through the offline CLI into the same tracker the gate observes."""
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    # This tracker also serves publication, where the person's --approve is allowed.
    case.tracker.actor = case.tracker.login = "human"
    case.tracker.strict = False
    case.events = []
    case.main = with_clean_preread(
        publish_cli((case.root, case.config, case.tracker, case.git, case.events), monkeypatch),
        monkeypatch,
    )
    return case


def finished_signals(case, number):
    return [event for event in events(case.store, "order-finished")
            if event.get("order") == number]


def assert_finished(case, number, work):
    record = case.tracker.get_issue(number)
    assert record.state == "closed", "The order must close once all of its work has closed"
    assert record.state_reason == "completed"
    comments = case.tracker.list_comments(number)
    assert len(comments) == 1, "The order must leave one closing roll-up"
    for issue in work:
        assert re.search(rf"(?:#|/issues/){issue}\b", comments[0].body), (
            f"The closing roll-up must name #{issue}: {comments[0].body}"
        )
    signals = finished_signals(case, number)
    assert len(signals) == 1, "The order-finished signal must arrive when the order closes"
    assert set(signals[0]["children"]) == set(work)


def test_edit_only_order_stays_open_while_an_edited_issue_is_open(publication, capsys):
    case = publication
    edited = (41, 42)
    for number in edited:
        case.tracker.add_issue(number, f"Existing work {number}", "The previous decision.")
    record = publish(case, capsys, edits=[{**EDIT, "number": number} for number in edited])
    assert all(case.tracker.get_issue(number).state == "open" for number in edited)

    gate_tick(case)
    assert case.tracker.get_issue(record.number).state == "open", (
        "An edit-only order must stay open while its edited issues #41 and #42 are open"
    )
    assert case.tracker.list_comments(record.number) == []
    assert finished_signals(case, record.number) == []

    case.tracker.close_issue(edited[0])
    gate_tick(case, refresh=True)
    assert case.tracker.get_issue(edited[1]).state == "open"
    assert case.tracker.get_issue(record.number).state == "open", (
        "Closing one edited issue must not finish an order with another edited issue open"
    )
    assert case.tracker.list_comments(record.number) == []
    assert finished_signals(case, record.number) == []

    case.tracker.close_issue(edited[1])
    gate_tick(case, refresh=True)
    assert_finished(case, record.number, edited)

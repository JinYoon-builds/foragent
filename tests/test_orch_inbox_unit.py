import json

import pytest

from foragent import orch_inbox
from foragent.orch_inbox import InboxError
from foragent.state import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


def test_requests_keep_order_and_pending_hides_done(store):
    first = orch_inbox.add_request(store, "planning", "one")
    second = orch_inbox.add_request(store, "planning", "two")
    orch_inbox.set_request_status(store, first["id"], "done", "finished")
    assert [row["id"] for row in orch_inbox.requests(store)] == [first["id"], second["id"]]
    assert [row["id"] for row in orch_inbox.requests(store, pending=True)] == [second["id"]]
    events = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events] == ["request", "request", "request-status"]


def test_request_status_rejects_unknown_status_and_path_like_ids(store):
    record = orch_inbox.add_request(store, "planning", "text")
    with pytest.raises(InboxError):
        orch_inbox.set_request_status(store, record["id"], "archived")
    for bad in ("../r1", ".r1", "r1/x"):
        with pytest.raises(InboxError):
            orch_inbox.set_request_status(store, bad, "done")
    with pytest.raises(InboxError):
        orch_inbox.add_request(store, "planning", "  ")


def test_ask_validates_recommendation_and_answer_closes_once(store):
    with pytest.raises(InboxError):
        orch_inbox.ask(store, "Which?", ["a", "b"], 3)
    with pytest.raises(InboxError):
        orch_inbox.ask(store, "Which?", [], 1)
    question = orch_inbox.ask(store, "Which?", ["a", "b"], 2)
    orch_inbox.answer(store, question["id"], "b")
    with pytest.raises(InboxError):
        orch_inbox.answer(store, question["id"], "a")
    closed = orch_inbox.questions(store, include_closed=True)
    assert closed[0]["answer"] == "b" and orch_inbox.questions(store) == []

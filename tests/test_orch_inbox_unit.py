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


def _clocked(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def test_planning_alive_needs_a_fresh_valid_heartbeat(tmp_path):
    from datetime import UTC, datetime, timedelta

    clock = [datetime(2026, 10, 24, tzinfo=UTC)]
    store = _clocked(tmp_path, clock)
    assert not orch_inbox.planning_alive(store)
    orch_inbox.write_plan_heartbeat(store)
    clock[0] += timedelta(seconds=120)
    assert orch_inbox.planning_alive(store)
    clock[0] += timedelta(seconds=1)
    assert not orch_inbox.planning_alive(store)
    for broken in ("not json", json.dumps({"at": "yesterday"}), json.dumps({"at": "2026-10-24T00:00:00"}),
                   json.dumps({})):
        orch_inbox.plan_heartbeat_path(store).write_text(broken)
        assert not orch_inbox.planning_alive(store)


def test_route_question_pushes_only_without_live_planning(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from foragent.config import Config

    clock = [datetime(2026, 10, 24, tzinfo=UTC)]
    store = _clocked(tmp_path, clock)
    sent = []
    monkeypatch.setattr(orch_inbox.notify, "send", lambda config, title, line, link=None: sent.append(
        (config.repo, title, line)) or True)
    record = orch_inbox.ask(store, "Widen scope?", ["yes", "no"], 1)
    assert orch_inbox.route_question(store, Config(language="ko"), "o/r", record)
    assert sent == [("o/r", "답할 질문이 있어요", f"{record['id']}: Widen scope?")]
    orch_inbox.write_plan_heartbeat(store)
    assert not orch_inbox.route_question(store, Config(), "o/r", record)
    assert len(sent) == 1


def test_brief_counts_open_questions_and_hides_zero_line(store):
    from foragent import brief

    assert brief.collect(store) == {"questions_open": 0}
    assert "답할 질문" not in brief.render(brief.collect(store), "ko")
    record = orch_inbox.ask(store, "Q?", [], None)
    assert brief.render(brief.collect(store), "ko") == "답할 질문 1개"
    assert brief.render(brief.collect(store), "en") == "Questions to answer: 1"
    orch_inbox.answer(store, record["id"], "ok")
    assert brief.collect(store) == {"questions_open": 0}

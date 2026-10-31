"""The one retirement rule and the derived views, beyond the locked observation-ingest tests."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, RecordingTracker, TrackerError

T0 = datetime(2026, 10, 31, 9, tzinfo=UTC)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def case(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(7, "Title", "Body", labels=["base"])
    events = []
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: clock[0])
    reader = RecordingTracker(remote, context, queue_writes=True)
    reader._outbox_dropped = lambda number, reason: events.append((number, reason))
    reader.list_issues()
    return SimpleNamespace(clock=clock, store=store, remote=remote, reader=reader, events=events)


def row(case):
    return {item["number"]: item for item in case.store.local_issues()}.get(7)


def write(case, action):
    return {"update_issue": lambda: case.reader.update_issue(7, title="Mine", body="Mine body"),
            "add_labels": lambda: case.reader.add_labels(7, ["mine"]),
            "close_issue": lambda: case.reader.close_issue(7)}[action]()


def effect_shown(case, action):
    current = row(case)
    return {"update_issue": current["title"] == "Mine", "add_labels": "mine" in current["labels"],
            "close_issue": current["state"] == "closed"}[action]


@pytest.mark.parametrize("action", ["update_issue", "add_labels", "close_issue"])
def test_rate_limited_write_survives_a_newer_read_of_the_unchanged_issue(case, action):
    def limited(*args, **kwargs):
        case.clock[0] = at(2)
        case.reader.get_issue(7)  # Started after the write; shows the issue unchanged.
        case.clock[0] = at(3)
        raise TrackerError("API rate limit exceeded (HTTP 403)")

    setattr(case.remote, action, limited)
    case.clock[0] = at(1)
    write(case, action)
    assert [entry["action"] for entry in case.store.github_outbox()] == [action]
    assert effect_shown(case, action), "The pending write stays visible on top of the observation"
    case.clock[0] = at(4)
    case.reader.get_issue(7)  # Still neither shown nor contradicted: stays pending to be retried.
    assert [entry["action"] for entry in case.store.github_outbox()] == [action]
    assert case.events == []


@pytest.mark.parametrize("action", ["update_issue", "add_labels", "close_issue"])
def test_a_persons_change_drops_a_pending_write_unsent(case, action):
    case.store.queue_github_write(7, action, **{"update_issue": {"title": "Mine", "body": "Mine body"},
                                                "add_labels": {"labels": ["mine"]}, "close_issue": {}}[action])
    case.clock[0] = at(1)
    case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"base", "needs-human"}), "open")
    case.reader.get_issue(7)
    assert case.store.github_outbox() == []
    assert case.events == [(7, "remote change")]
    assert row(case)["labels"] == ["base", "needs-human"] and row(case)["title"] == "Title"


def test_a_sent_write_lives_until_a_read_started_after_its_send(case):
    entry = case.store.queue_github_write(7, "add_labels", labels=["mine"])
    case.store.record_github_attempt(entry["id"], at(3), True)
    assert case.store.github_outbox() == [] and "mine" in row(case)["labels"]
    case.store.ingest([Issue(7, "Title", "Body", frozenset({"base"}), "open")], at(2), {})
    assert "mine" in row(case)["labels"], "A read that started before the send cannot see it"
    case.store.ingest([Issue(7, "Title", "Body", frozenset({"base"}), "open")], at(4), {})
    assert row(case)["labels"] == ["base"], "A read started after the send retires it"


def test_a_failed_send_is_not_a_send(case):
    entry = case.store.queue_github_write(7, "add_labels", labels=["mine"])
    case.store.record_github_attempt(entry["id"], at(1), False)
    case.store.ingest([Issue(7, "Title", "Body", frozenset({"base"}), "open")], at(2), {})
    assert [item["id"] for item in case.store.github_outbox()] == [entry["id"]]


def test_absence_retires_pending_writes_queued_before_it(case):
    case.store.queue_github_write(7, "add_labels", labels=["early"])
    case.clock[0] = at(3)
    case.store.queue_github_write(7, "add_labels", labels=["late"])
    assert case.store.ingest_absence(7, at(2)) is True
    assert [item["labels"] for item in case.store.github_outbox()] == [["late"]]


def test_visible_issue_and_outbox_are_derived_not_stored(case):
    case.store.queue_github_write(7, "add_labels", labels=["mine"])
    saved = json.loads((case.store.root / "issues.json").read_text(encoding="utf-8"))
    assert saved["issues"]["7"]["labels"] == ["base"], "The observation stays as read"
    assert "mine" in row(case)["labels"], "The visible issue adds the unretired intent"
    assert saved["outbox"][0]["premise"]["labels"] == ["base"]
    assert "premise" not in case.store.github_outbox()[0]


def raw_intents(case):
    return json.loads((case.store.root / "issues.json").read_text(encoding="utf-8")).get("outbox", [])


@pytest.mark.parametrize("ack", ["update_local_issue", "record_labels"])
def test_a_late_acknowledgment_does_not_bring_back_a_missing_issue(case, ack):
    case.store.ingest_absence(7, at(2))
    if ack == "update_local_issue":
        case.store.update_local_issue(7, observed_at=at(1), title="Mine")
    else:
        case.store.record_labels(7, ["mine"], remove=False, lanes={}, observed_at=at(1))
    assert row(case) is None


def test_a_direct_write_whose_issue_vanished_during_it_stays_gone(case):
    update = case.remote.update_issue

    def deleted(*args, **kwargs):
        update(*args, **kwargs)
        case.clock[0] = at(2)
        case.remote.issues.pop(7)
        with pytest.raises(TrackerError):
            case.reader.get_issue(7)
        case.clock[0] = at(3)

    case.remote.get_issue = lambda n: case.remote.issues[n] if n in case.remote.issues else (
        _ for _ in ()).throw(TrackerError("gh: not found (HTTP 404)"))
    case.remote.update_issue = deleted
    case.clock[0] = at(1)
    case.reader.update_issue(7, title="Mine", body="Mine body")
    assert row(case) is None


@pytest.mark.parametrize("newer", ["complete listing", "person"])
def test_a_late_queued_write_is_judged_by_the_whole_latest_observation(case, newer):
    other = Issue(8, "Other", "", frozenset(), "open")

    def limited(*args, **kwargs):
        case.clock[0] = at(2)
        if newer == "complete listing":
            case.store.ingest([other], at(2), {}, complete=True)
        else:
            case.store.ingest([Issue(7, "Title", "Body", frozenset({"base", "needs-human"}), "open")], at(2), {})
        case.clock[0] = at(3)
        raise TrackerError("API rate limit exceeded (HTTP 403)")

    case.remote.add_labels = limited
    case.clock[0] = at(1)
    case.reader.add_labels(7, ["mine"])
    assert case.store.github_outbox() == []
    if newer == "complete listing":
        assert row(case) is None and case.events == [(7, "issue not found")]
    else:
        assert row(case)["labels"] == ["base", "needs-human"] and case.events == [(7, "remote change")]


def test_a_read_made_during_a_send_judges_it_once_the_send_completes(case):
    case.store.queue_github_write(7, "update_issue", title="Mine", body="Mine body")
    update = case.remote.update_issue

    def slow(*args, **kwargs):
        update(*args, **kwargs)
        case.clock[0] = at(2)  # A person edits; a read started during the send sees it.
        case.remote.issues[7] = Issue(7, "Person", "Person body", frozenset({"base"}), "open")
        case.reader.get_issue(7)
        case.clock[0] = at(3)

    case.remote.update_issue = slow
    case.clock[0] = at(1)
    case.reader.flush_outbox()
    assert case.store.github_outbox() == [] and raw_intents(case) == []
    assert row(case)["title"] == "Person"


def test_every_failed_send_is_recorded(case):
    entry = case.store.queue_github_write(7, "add_labels", labels=["mine"])

    def broken(*args):
        raise TrackerError("gh: server error (HTTP 500)")

    case.remote.add_labels = broken
    case.clock[0] = at(1)
    with pytest.raises(TrackerError, match="500"):
        case.reader.flush_outbox()
    attempts = next(intent for intent in raw_intents(case) if intent["id"] == entry["id"])["attempts"]
    assert attempts == [{"started_at": at(1).isoformat(), "ok": False}]
    assert [item["id"] for item in case.store.github_outbox()] == [entry["id"]]


def test_a_read_older_than_a_partial_send_keeps_the_rest_of_the_removal_pending(case):
    case.store.ingest([Issue(7, "Title", "Body", frozenset({"a", "b"}), "open")], T0, {})
    entry = case.store.queue_github_write(7, "remove_labels", labels=["a", "b"])
    case.store.record_github_attempt(entry["id"], at(3), True, labels=["a"])
    case.store.record_github_attempt(entry["id"], at(4), False, labels=["b"])
    # Started before the send of "a": it still shows the premise unchanged, so no person changed anything.
    assert case.store.ingest([Issue(7, "Title", "Body", frozenset({"a", "b"}), "open")], at(2), {}) == []
    assert [item["labels"] for item in case.store.github_outbox()] == [["b"]]
    # Started after the send of "a": "a" is gone by our own send, still no person's change.
    assert case.store.ingest([Issue(7, "Title", "Body", frozenset({"b"}), "open")], at(5), {}) == []
    assert [item["labels"] for item in case.store.github_outbox()] == [["b"]]

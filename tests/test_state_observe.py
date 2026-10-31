"""Observation ordering cases beyond the locked observation-ingest acceptance tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, RecordingTracker, TrackerError

T0 = datetime(2026, 10, 31, 9, tzinfo=UTC)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def issue(title="Title", labels=(), state="open"):
    return Issue(7, title, "Body", frozenset(labels), state)


@pytest.fixture
def store(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    store.observe_issues([issue(), Issue(8, "Other", "", frozenset(), "open")], T0, {}, complete=True)
    return store


def row(store):
    return {item["number"]: item for item in store.local_issues()}.get(7)


@pytest.mark.parametrize("bulk", [False, True])
def test_repeated_complete_absence_advances_so_older_presence_stays_out(store, bulk):
    other = Issue(8, "Other", "", frozenset(), "open")

    def listing(issues, when):
        if bulk:
            store.refresh_local_issues(issues, [], {}, when)
        else:
            store.observe_issues(issues, when, {}, complete=True)

    listing([other], at(1))
    listing([other], at(3))
    store.observe_issues([issue("Late")], at(2), {})
    assert row(store) is None


def test_acknowledgment_after_newer_absence_does_not_recreate_issue(store):
    store.observe_absence(7, at(2))
    for action, payload in (("add_labels", {"labels": ["x"]}), ("update_issue", {"title": "t", "body": "b"}),
                            ("close_issue", {})):
        store.acknowledge_github_write(7, action, at(1), lanes={}, **payload)
        assert row(store) is None


def test_direct_write_overlay_survives_older_read_until_a_read_after_its_start(store):
    store.acknowledge_github_write(7, "add_labels", at(2), lanes={}, labels=["mine"])
    store.observe_issues([issue("Older read")], at(1), {})
    assert row(store)["labels"] == ["mine"] and row(store)["title"] == "Older read"
    store.observe_issues([issue("Newer read")], at(3), {})
    assert row(store)["labels"] == [] and row(store)["title"] == "Newer read"


def test_contradiction_drops_only_writes_started_before_it(store):
    store.clock[0] = at(1)
    store.queue_github_write(7, "add_labels", labels=["early"])
    store.clock[0] = at(3)
    store.queue_github_write(7, "add_labels", labels=["late"])
    dropped = store.observe_issues([issue(labels=["human"])], at(2), {})
    assert dropped == [7]
    assert [entry["labels"] for entry in store.github_outbox()] == [["late"]]
    assert row(store)["labels"] == ["human", "late"]


def test_independent_confirmed_write_retires_behind_an_unconfirmed_one(store):
    store.clock[0] = at(1)
    store.queue_github_write(7, "add_labels", labels=["a"])
    store.queue_github_write(7, "remove_labels", labels=["b"])
    store.queue_github_write(7, "remove_labels", labels=["a"])
    store.observe_issues([issue()], at(2), {})
    # Removing absent "b" is confirmed; removing "a" waits behind adding "a".
    assert [(entry["action"], entry["labels"]) for entry in store.github_outbox()] == [
        ("add_labels", ["a"]), ("remove_labels", ["a"])]


def test_write_queued_after_absence_keeps_its_row_and_overlay(store):
    store.clock[0] = at(3)
    store.queue_github_write(7, "add_labels", labels=["late"])
    assert store.observe_absence(7, at(2)) is False
    assert [entry["labels"] for entry in store.github_outbox()] == [["late"]]
    assert row(store)["labels"] == ["late"]
    store.observe_issues([issue("Old")], at(1), {})
    assert row(store)["labels"] == ["late"] and row(store)["title"] == "Title"


def test_flush_404_is_an_ordered_absence(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(7, "Title")
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: clock[0])
    reader = RecordingTracker(remote, context, queue_writes=True)
    reader.list_issues()
    store.queue_github_write(7, "add_labels", labels=["x"])
    clock[0] = at(1)

    def gone(*args):
        raise TrackerError("gh: not found (HTTP 404)")

    def delayed_missing(number):
        # A newer listing is ingested while this 404 lookup waits.
        clock[0] = at(2)
        remote.issues[7] = Issue(7, "Newer", "", frozenset({"x"}), "open")
        reader.list_issues()
        clock[0] = at(3)
        raise TrackerError("gh: not found (HTTP 404)")

    remote.add_labels = gone
    remote.get_issue = delayed_missing
    reader.flush_outbox()
    assert row(store)["title"] == "Newer"
    remote.get_issue = lambda number: (_ for _ in ()).throw(TrackerError("gh: not found (HTTP 404)"))
    clock[0] = at(4)
    store.queue_github_write(7, "add_labels", labels=["y"])
    clock[0] = at(5)
    reader.flush_outbox()
    assert row(store) is None and store.github_outbox() == []


def test_rate_limited_direct_write_is_queued_from_its_start(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(7, "Title")
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: clock[0])
    reader = RecordingTracker(remote, context, queue_writes=True)

    def limited(*args):
        clock[0] = at(5)
        raise TrackerError("API rate limit exceeded (HTTP 403)")

    remote.add_labels = limited
    reader.add_labels(7, ["x"])
    assert [entry["queued_at"] for entry in store.github_outbox()] == [T0.isoformat()]


@pytest.mark.parametrize("write", ["acknowledged", "queued"])
def test_refresh_keeps_new_facts_under_a_write_made_during_it(store, write):
    revision = store.issues_revision()
    store.clock[0] = at(3)
    if write == "acknowledged":
        store.acknowledge_github_write(7, "add_labels", at(3), lanes={}, labels=["mine"])
    else:
        store.queue_github_write(7, "add_labels", labels=["mine"])
    store.refresh_local_issues([issue("New title"), Issue(8, "Other", "", frozenset(), "open")], [], {}, at(2),
                               since_revision=revision)
    assert (row(store)["title"], row(store)["labels"], row(store)["observed_at"]) == (
        "New title", ["mine"], at(2).isoformat())


def recording(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(7, "Title")
    events = []
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: clock[0])
    reader = RecordingTracker(remote, context, queue_writes=True)
    reader._outbox_dropped = lambda number, reason: events.append((number, reason))
    reader.list_issues()
    return SimpleNamespace(clock=clock, store=store, remote=remote, reader=reader, events=events)


@pytest.mark.parametrize("action", ["add_labels", "update_issue", "close_issue"])
def test_rate_limited_write_judged_by_a_newer_read_is_not_queued(tmp_path, action):
    case = recording(tmp_path)
    case.clock[0] = at(1)

    def limited(*args, **kwargs):
        case.clock[0] = at(2)
        case.remote.issues[7] = Issue(7, "Human", "Human body", frozenset({"human"}), "open")
        case.reader.get_issue(7)
        case.clock[0] = at(3)
        raise TrackerError("API rate limit exceeded (HTTP 403)")

    setattr(case.remote, action, limited)
    {"add_labels": lambda: case.reader.add_labels(7, ["mine"]),
     "update_issue": lambda: case.reader.update_issue(7, title="Mine", body="Mine"),
     "close_issue": lambda: case.reader.close_issue(7)}[action]()
    assert case.store.github_outbox() == []
    assert (row(case.store)["title"], row(case.store)["labels"], row(case.store)["state"]) == (
        "Human", ["human"], "open")
    assert case.events == [(7, "remote change")]


@pytest.mark.parametrize("action,payload", [("add_labels", {"labels": ["x"]}), ("comment", {"body": "x"})])
def test_write_started_before_a_newer_absence_is_not_queued(store, action, payload):
    store.observe_absence(7, at(2))
    assert store.queue_github_write(7, action, started_at=at(1), **payload) is None
    assert row(store) is None and store.github_outbox() == []


def test_delayed_create_acknowledgment_cannot_replace_newer_facts(tmp_path):
    case = recording(tmp_path)
    create = case.remote.create_issue

    def delayed(title, body, labels):
        number = create(title, body, labels)
        case.clock[0] = at(2)
        case.remote.issues[number] = Issue(number, "Edited", "Edited body", frozenset({"human"}), "closed",
                                           state_reason="not_planned")
        case.reader.list_issues()
        case.clock[0] = at(3)
        return number

    case.clock[0] = at(1)
    case.remote.create_issue = delayed
    number = case.reader.create_issue("Created", "Body", ["new"])
    created = {item["number"]: item for item in case.store.local_issues()}[number]
    assert (created["title"], created["labels"], created["state"], created["state_reason"]) == (
        "Edited", ["human"], "closed", "not_planned")


OTHER = Issue(8, "Other", "", frozenset(), "open")


def test_older_read_finishing_during_a_newer_refresh_does_not_win(store):
    revision = store.issues_revision()
    store.observe_issues([issue("T1 read")], at(1), {})  # Started before the refresh, saved during it.
    store.refresh_local_issues([issue("T2 listing"), OTHER], [], {}, at(2), since_revision=revision)
    assert (row(store)["title"], row(store)["observed_at"]) == ("T2 listing", at(2).isoformat())


@pytest.mark.parametrize("action,payload", [
    ("add_labels", {"labels": ["mine"]}), ("update_issue", {"title": "Mine", "body": "Mine"}), ("close_issue", {})])
def test_older_write_acknowledged_during_a_newer_refresh_is_judged_by_it(store, action, payload):
    revision = store.issues_revision()
    store.acknowledge_github_write(7, action, at(1), lanes={}, **payload)
    human = Issue(7, "Human", "Human body", frozenset({"human"}), "open")
    store.refresh_local_issues([human, OTHER], [], {}, at(2), since_revision=revision)
    assert (row(store)["title"], row(store)["labels"], row(store)["state"]) == ("Human", ["human"], "open")


def test_older_queued_write_saved_during_a_newer_refresh_is_judged_by_it(store):
    revision = store.issues_revision()
    store.queue_github_write(7, "add_labels", labels=["mine"], started_at=at(1))
    human = Issue(7, "Title", "Body", frozenset({"human"}), "open")
    assert store.refresh_local_issues([human, OTHER], [], {}, at(2), since_revision=revision) == [7]
    assert store.github_outbox() == [] and row(store)["labels"] == ["human"]


@pytest.mark.parametrize("bulk", [False, True])
def test_complete_listing_shows_issues_without_a_row_missing(store, bulk):
    if bulk:
        store.refresh_local_issues([issue(), OTHER], [], {}, at(2))
    else:
        store.observe_issues([issue(), OTHER], at(2), {}, complete=True)
    late = Issue(9, "Late", "", frozenset(), "open")
    store.observe_issues([late], at(1), {})
    store.refresh_local_issues([issue(), OTHER, late], [], {}, at(1))
    store.acknowledge_github_write(9, "add_labels", at(1), lanes={}, labels=["x"])
    assert store.queue_github_write(9, "add_labels", labels=["x"], started_at=at(1)) is None
    assert 9 not in {item["number"] for item in store.local_issues()}
    store.observe_issues([late], at(3), {})
    assert 9 in {item["number"] for item in store.local_issues()}


def test_partial_delivery_during_a_refresh_is_not_a_contradiction(store):
    store.observe_issues([issue(labels=["a", "b"]), OTHER], T0, {}, complete=True)
    store.clock[0] = at(1)
    entry = store.queue_github_write(7, "remove_labels", labels=["a", "b"])
    revision = store.issues_revision()
    store.deliver_github_write(entry, remaining_labels=["b"], started_at=at(3))
    assert store.refresh_local_issues([issue(labels=["a", "b"]), OTHER], [], {}, at(2),
                                      since_revision=revision) == []
    assert [item["labels"] for item in store.github_outbox()] == [["b"]] and row(store)["labels"] == []


@pytest.mark.parametrize("action", ["update_issue", "remove_labels", "add_labels"])
def test_flush_sends_nothing_a_newer_read_dropped_while_a_delivery_waited(tmp_path, action):
    case = recording(tmp_path)
    case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"a", "b"}), "open")
    case.reader.list_issues()
    case.clock[0] = at(1)
    writes = {"update_issue": [{"title": "Mine 1", "body": "x"}, {"title": "Mine 2", "body": "y"}],
              "remove_labels": [{"labels": ["a", "b"]}],
              "add_labels": [{"labels": ["c"]}, {"labels": ["d"]}]}[action]
    for payload in writes:
        case.store.queue_github_write(7, action, **payload)
    human = Issue(7, "Human", "Human body", frozenset({"human"}), "open")
    sent = []
    original = getattr(case.remote, action)

    def slow(number, *args, **kwargs):
        sent.append((args, kwargs))
        original(number, *args, **kwargs)
        if len(sent) == 1:
            # A newer read lands while the first send waits; it contradicts every queued write.
            case.clock[0] = at(2)
            case.remote.issues[7] = human
            case.reader.get_issue(7)
            case.clock[0] = at(3)

    setattr(case.remote, action, slow)
    case.reader.flush_outbox()
    assert len(sent) == 1, "A write dropped by a newer read must not be sent"
    assert case.store.github_outbox() == []
    assert (row(case.store)["title"], row(case.store)["labels"]) == ("Human", ["human"])


@pytest.mark.parametrize("bulk", [False, True])
def test_pending_write_is_judged_against_the_latest_read_not_the_first_queue(store, bulk):
    def read(seen, when):
        if bulk:
            return store.refresh_local_issues([seen, OTHER], [], {}, when)
        return store.observe_issues([seen], when, {})

    store.clock[0] = at(1)
    store.queue_github_write(7, "add_labels", labels=["x"])
    human = issue("Human")
    read(human, at(2))  # Not the label write's field: it stays pending.
    store.clock[0] = at(3)
    store.queue_github_write(7, "update_issue", title="Mine", body="Mine body")
    assert read(human, at(4)) == []  # Same human title as the edit was based on.
    assert [entry["action"] for entry in store.github_outbox()] == ["add_labels", "update_issue"]
    assert (row(store)["title"], row(store)["labels"]) == ("Mine", ["x"])


@pytest.mark.parametrize("local", ["queue", "acknowledge", "update_local_issue"])
@pytest.mark.parametrize("bulk", [False, True])
def test_local_row_for_an_omitted_issue_does_not_hide_the_omission(store, local, bulk):
    store.observe_issues([issue(), OTHER], at(2), {}, complete=True)
    store.clock[0] = at(3)
    if local == "queue":
        store.queue_github_write(9, "comment", body="Note")
    elif local == "acknowledge":
        store.acknowledge_github_write(9, "add_labels", at(3), lanes={}, labels=["x"])
    else:
        store.update_local_issue(9, pr_number=4)
    stale = Issue(9, "Stale", "Stale body", frozenset({"old"}), "closed")
    if bulk:
        store.refresh_local_issues([issue(), OTHER, stale], [], {}, at(1))
    else:
        store.observe_issues([stale], at(1), {})
    assert {item["number"]: item for item in store.local_issues()}[9]["title"] != "Stale"
    store.acknowledge_github_write(9, "update_issue", at(1), lanes={}, title="Late", body="Late")
    assert {item["number"]: item for item in store.local_issues()}[9]["title"] != "Late"

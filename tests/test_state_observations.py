"""The one retirement rule and the derived views, beyond the locked observation-ingest tests."""

from datetime import UTC, datetime, timedelta
import json
import threading
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
        case.clock[0] = at(2)  # A read during the send reports what it drops.
        if newer == "complete listing":
            case.remote.issues = {8: other}
            case.reader.list_issues()
        else:
            case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"base", "needs-human"}), "open")
            case.reader.get_issue(7)
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


READS = {
    "list_issues": lambda case: case.reader.list_issues(),
    "list_issues_open": lambda case: case.reader.list_issues("open"),
    "list_open_issues": lambda case: case.reader.list_open_issues(),
    "issues_closed_since": lambda case: case.reader.issues_closed_since(T0),
    "get_issue": lambda case: case.reader.get_issue(7),
    "sub_issues": lambda case: case.reader.sub_issues(9),
    "parent_issue": lambda case: case.reader.parent_issue(10),
    "observe_closed_issues": lambda case: case.reader.observe_closed_issues(observed_at=case.clock[0]),
    "refresh_issues": lambda case: case.reader.refresh_issues(),
}


@pytest.mark.parametrize("read", sorted(READS))
def test_every_read_returning_issues_goes_through_ingest(case, read):
    case.remote.add_issue(9, "Parent")
    case.remote.add_sub_issue(9, 7)
    case.remote.add_issue(10, "Child")
    case.remote.set_parent(10, 7)
    case.store.record_closed_issue_watermark(T0)
    case.store.queue_github_write(7, "add_labels", labels=["mine"])
    closed = read in ("issues_closed_since", "observe_closed_issues")
    case.clock[0] = at(1)
    case.remote.issues[7] = Issue(7, "Newer", "Body", frozenset({"base", "mine"}), "closed" if closed else "open",
                                  updated_at=at(1).isoformat())
    READS[read](case)
    assert row(case)["title"] == "Newer", f"{read} must record the newer facts"
    assert case.store.github_outbox() == [], f"{read} must retire the intent its facts confirm"


def saved_rows(case):
    return json.loads((case.store.root / "issues.json").read_text(encoding="utf-8"))["issues"]


def test_an_older_observation_in_a_listing_persists_nothing_for_that_issue(case):
    case.store.ingest([Issue(7, "Newest", "Body", frozenset({"base"}), "open")], at(5), {})
    before = saved_rows(case)["7"]
    case.store.ingest([Issue(7, "Older", "Body", frozenset({"base"}), "open"), Issue(8, "New", "", frozenset(), "open")],
                      at(2), {})
    assert saved_rows(case)["7"] == before, "No seen-stamp, revision or updated_at for a rejected observation"
    assert saved_rows(case)["8"]["title"] == "New"


@pytest.mark.parametrize("local", ["facts", "labels", "facts_and_field"])
def test_an_older_local_update_persists_nothing_of_its_facts(case, local):
    case.store.ingest([Issue(7, "Newest", "Body", frozenset({"base"}), "open")], at(5), {})
    path = case.store.root / "issues.json"
    before, rows = path.read_bytes(), saved_rows(case)["7"]
    if local == "facts":
        case.store.update_local_issue(7, observed_at=at(2), title="Older")
    elif local == "labels":
        case.store.record_labels(7, ["ready"], remove=False, lanes={}, observed_at=at(2))
    else:
        case.store.update_local_issue(7, observed_at=at(2), title="Older", pr_number=4)
    if local == "facts_and_field":
        after = saved_rows(case)["7"]
        assert (after["title"], after["observed_at"], after["pr_number"]) == ("Newest", rows["observed_at"], 4)
    else:
        assert path.read_bytes() == before


@pytest.mark.parametrize("bulk", [False, True])
def test_a_complete_listing_retires_an_intent_queued_for_an_issue_already_gone(case, bulk):
    other = Issue(8, "Other", "", frozenset(), "open")
    case.store.ingest_absence(7, at(1))
    case.clock[0] = at(2)
    case.store.queue_github_write(7, "add_labels", labels=["late"])  # A write after the absence counts.
    if bulk:
        dropped = case.store.refresh_local_issues([other], [], {}, at(3))
    else:
        dropped = case.store.ingest([other], at(3), {}, complete=True)
    assert dropped == [7] and case.store.github_outbox() == []


@pytest.mark.parametrize("older", ["absence", "complete listing"])
def test_an_absence_older_than_a_stored_absence_persists_nothing(case, older):
    case.store.ingest_absence(7, at(3))
    path = case.store.root / "issues.json"
    before = path.read_bytes()
    if older == "absence":
        assert case.store.ingest_absence(7, at(1)) is False
    else:
        case.store.ingest([Issue(8, "Other", "", frozenset(), "open")], at(5), {}, complete=True)
        before = path.read_bytes()
        case.store.ingest([Issue(8, "Other", "", frozenset(), "open")], at(4), {}, complete=True)
    assert path.read_bytes() == before


def approve(store, at_time):
    store.save_approval(7, body="Body", body_sha="sha", acceptance_blobs={}, approved_by="person",
                        approved_at=at_time.isoformat())


@pytest.mark.parametrize("path", ["queued", "direct"])
def test_a_ready_label_belongs_to_the_approval_current_when_its_write_started(case, path):
    first, second = at(1), at(2)
    approve(case.store, first)
    if path == "queued":
        case.store.queue_github_write(7, "add_labels", labels=["ready"])
        approve(case.store, second)  # Reapproved while the write is pending.
        assert row(case)["ready_approval"] == first.isoformat()
        case.store.ingest([Issue(7, "Title", "Body", frozenset({"base", "ready"}), "open")], at(3), {})
        assert case.store.github_outbox() == []
    else:
        add = case.remote.add_labels

        def reapproved(*args):
            result = add(*args)
            approve(case.store, second)  # Reapproved while the send is in flight.
            return result

        case.remote.add_labels = reapproved
        case.reader.add_labels(7, ["ready"])
    assert row(case)["ready_approval"] == first.isoformat()


@pytest.mark.parametrize("action", ["add_labels", "remove_labels", "close_issue"])
def test_a_write_queued_during_a_direct_send_sees_that_send_in_its_premise(case, action):
    case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"base", "gone"}), "open")
    case.reader.list_issues()
    send = getattr(case.remote, action)

    def slow(*args, **kwargs):
        send(*args, **kwargs)
        case.clock[0] = at(2)  # Another writer queues a comment while this send waits.
        case.store.queue_github_write(7, "comment", body="Evidence")
        case.clock[0] = at(3)
        return None

    setattr(case.remote, action, slow)
    case.clock[0] = at(1)
    {"add_labels": lambda: case.reader.add_labels(7, ["mine"]),
     "remove_labels": lambda: case.reader.remove_labels(7, ["gone"]),
     "close_issue": lambda: case.reader.close_issue(7)}[action]()
    case.clock[0] = at(4)
    case.reader.get_issue(7)  # GitHub now shows the delivered write and nothing else.
    assert [entry["action"] for entry in case.store.github_outbox()] == ["comment"]
    assert case.events == []


@pytest.mark.parametrize("local", ["update_local_issue", "record_labels", "direct update"])
def test_a_change_made_after_an_absence_counts_as_newer(case, local):
    case.store.ingest_absence(7, at(1))
    case.clock[0] = at(2)
    if local == "update_local_issue":
        case.store.update_local_issue(7, observed_at=at(2), title="Back")
        assert row(case)["title"] == "Back"
    elif local == "record_labels":
        case.store.record_labels(7, ["back"], remove=False, lanes={}, observed_at=at(2))
        assert row(case)["labels"] == ["back"]
    else:
        case.reader.update_issue(7, title="Back", body="Back body")
        assert row(case)["title"] == "Back"


def test_a_direct_send_that_fails_for_good_is_neither_visible_nor_retried(case):
    def broken(*args):
        raise TrackerError("gh: validation failed (HTTP 422)")

    case.remote.add_labels = broken
    case.clock[0] = at(1)
    with pytest.raises(TrackerError, match="422"):
        case.reader.add_labels(7, ["mine"])
    assert case.store.github_outbox() == [] and row(case)["labels"] == ["base"]
    case.clock[0] = at(2)
    case.reader.get_issue(7)
    assert raw_intents(case) == [], "The next read clears the abandoned intent from the log"


@pytest.mark.parametrize("action", ["comment", "close_issue"])
def test_a_rejected_post_leaves_no_comment_fact_before_or_after_a_read(case, action):
    def rejected(*args, **kwargs):
        raise TrackerError("gh: validation failed (HTTP 422)")

    setattr(case.remote, action, rejected)
    before = (case.store.root / "issues.json").read_bytes()
    case.clock[0] = at(1)
    with pytest.raises(TrackerError, match="422"):
        getattr(case.reader, action)(7, "Unposted evidence")
    assert row(case)["comments"] == []
    assert case.store.github_outbox() == []
    if action == "comment":
        assert (case.store.root / "issues.json").read_bytes() == before
    case.clock[0] = at(2)
    case.reader.get_issue(7)
    assert row(case)["comments"] == [] and raw_intents(case) == []


@pytest.mark.parametrize("action", ["add_labels", "remove_labels"])
@pytest.mark.parametrize("noop", [False, True])
def test_an_abandoned_ready_write_never_changes_approval_attribution(case, action, noop):
    if (action == "remove_labels") != noop:
        case.remote.add_labels(7, ["ready"])
        case.reader.get_issue(7)
    case.store.update_local_issue(7, ready_approval="original approval")
    approve(case.store, at(1))

    def rejected(*args):
        raise TrackerError("gh: validation failed (HTTP 422)")

    setattr(case.remote, action, rejected)
    case.clock[0] = at(2)
    with pytest.raises(TrackerError, match="422"):
        getattr(case.reader, action)(7, ["ready"])
    assert row(case)["ready_approval"] == "original approval"
    case.clock[0] = at(3)
    case.reader.get_issue(7)
    assert row(case)["ready_approval"] == "original approval"


@pytest.mark.parametrize("action", ["comment", "close_issue"])
def test_queued_posts_never_invent_comment_facts(case, action):
    entry = case.store.queue_github_write(7, action, body="Queued evidence")
    assert row(case)["comments"] == []
    if action == "close_issue":
        assert row(case)["state"] == "closed"
    case.clock[0] = at(1)
    case.reader.get_issue(7)
    assert row(case)["comments"] == []
    assert [item["id"] for item in case.store.github_outbox()] == [entry["id"]]


def test_a_queued_comment_ends_on_ack_with_the_real_id_even_if_a_read_interleaves(case):
    case.store.queue_github_write(7, "comment", body="Evidence")
    post = case.remote.comment

    def observed(n, body):
        identity = post(n, body)
        case.clock[0] = at(2)
        case.reader.get_issue(n)  # This is not a comment acknowledgement.
        assert [item["action"] for item in case.store.github_outbox()] == ["comment"]
        case.reader.list_comments(n)  # The read already contains the real id.
        return identity

    case.remote.comment = observed
    case.clock[0] = at(1)
    case.reader.flush_outbox()
    assert raw_intents(case) == [] and case.store.github_outbox() == []
    assert [(item["id"], item["body"]) for item in row(case)["comments"]] == [(1, "Evidence")]


def test_a_failed_queued_comment_stays_send_only_until_ack_or_drop(case):
    entry = case.store.queue_github_write(7, "comment", body="Evidence")

    def rejected(*args):
        raise TrackerError("gh: validation failed (HTTP 422)")

    case.remote.comment = rejected
    case.clock[0] = at(1)
    with pytest.raises(TrackerError, match="422"):
        case.reader.flush_outbox()
    assert row(case)["comments"] == [] and case.store.github_outbox() == [entry]
    assert "attempts" not in raw_intents(case)[0]
    case.clock[0] = at(2)
    case.reader.get_issue(7)
    assert case.store.github_outbox() == [entry] and row(case)["comments"] == []
    case.store.ingest_absence(7, at(3))
    assert case.store.github_outbox() == []


def test_a_direct_comment_is_recorded_only_from_its_successful_returned_id(case):
    assert case.reader.comment(7, "Posted evidence") == 1
    assert raw_intents(case) == []
    assert [(item["id"], item["body"]) for item in row(case)["comments"]] == [(1, "Posted evidence")]


def test_a_direct_close_does_not_expose_its_comment_to_a_concurrent_flush(case):
    close = case.remote.close_issue
    active = [False]

    def slow(n, comment=None):
        if comment is not None and not active[0]:
            active[0] = True
            case.clock[0] = at(2)
            case.reader.flush_outbox()
            case.clock[0] = at(3)
        close(n, comment)

    case.remote.close_issue = slow
    case.clock[0] = at(1)
    case.reader.close_issue(7, "Closed with evidence")
    assert [item.body for item in case.remote.list_comments(7)] == ["Closed with evidence"]
    assert row(case)["state"] == "closed"
    assert case.store.github_outbox() == []


@pytest.mark.parametrize("path", ["queued", "direct"])
def test_a_close_comment_becomes_a_fact_only_when_read(case, path):
    if path == "queued":
        case.store.queue_github_write(7, "close_issue", body="Closing evidence")
        case.reader.flush_outbox()
    else:
        case.reader.close_issue(7, "Closing evidence")
    assert row(case)["comments"] == []
    case.clock[0] = at(1)
    case.reader.refresh_issues()
    assert [(item["id"], item["body"]) for item in row(case)["comments"]] == [(1, "Closing evidence")]


@pytest.mark.parametrize("action", ["add_labels", "remove_labels"])
def test_a_sent_ready_write_cannot_attribute_an_effect_the_read_does_not_show(case, action):
    if action == "remove_labels":
        case.remote.add_labels(7, ["ready"])
        case.reader.get_issue(7)
    case.store.update_local_issue(7, ready_approval="original approval")
    approve(case.store, at(1))
    entry = case.store.queue_github_write(7, action, labels=["ready"])
    case.store.record_github_attempt(entry["id"], at(1), True)
    case.clock[0] = at(2)
    case.reader.get_issue(7)  # Retires by send time, but shows no ready-label effect.
    assert case.store.github_outbox() == [] and raw_intents(case) == []
    assert row(case)["ready_approval"] == "original approval"


@pytest.mark.parametrize("known", [False, True])
def test_queueing_a_comment_changes_neither_issue_facts_nor_provenance(case, known):
    before = case.store.local_issues()
    case.clock[0] = at(1)
    number = 7 if known else 8
    case.store.queue_github_write(number, "comment", body="Unsent")
    assert case.store.local_issues() == before
    assert case.store.github_outbox()[0]["issue"] == number


@pytest.mark.parametrize("change", ["labels", "state"])
def test_evidence_survives_workflow_changes_but_ordinary_comments_do_not(case, change):
    evidence = case.store.queue_github_write(7, "comment", body="  Exact\n\n advice  ", evidence=True)
    case.store.queue_github_write(7, "comment", body="Superseded transition")
    case.clock[0] = at(1)
    issue = case.remote.get_issue(7)
    from dataclasses import replace
    case.remote.issues[7] = replace(issue, **({"labels": frozenset({"human"})} if change == "labels"
                                             else {"state": "closed"}))
    case.reader.get_issue(7)
    assert case.store.github_outbox() == [evidence]
    assert case.store.github_blocking_outbox() == []
    assert case.store.github_write_issues() == set()
    assert row(case)["comments"] == []
    expected = case.remote.get_issue(7)
    case.reader.flush_outbox()
    case.reader.flush_outbox()
    assert [comment.body for comment in case.remote.list_comments(7)] == [evidence["body"]]
    assert case.store.github_outbox() == []
    assert replace(case.remote.get_issue(7), updated_at=expected.updated_at) == expected


def test_missing_issue_still_retires_evidence(case):
    case.store.queue_github_write(7, "comment", body="Advice", evidence=True)
    case.store.ingest_absence(7, at(1))
    assert case.store.github_outbox() == []


ORDINARY_WRITES = {"add_labels": {"labels": ["mine"]}, "remove_labels": {"labels": ["base"]}, "close_issue": {},
                   "update_issue": {"title": "Mine", "body": "Mine body"}}


@pytest.mark.parametrize("action", sorted(ORDINARY_WRITES))
def test_an_ordinary_write_given_no_premise_is_still_judged_against_the_visible_issue(case, action):
    case.store.queue_github_write(7, action, premise=None, **ORDINARY_WRITES[action])
    assert raw_intents(case)[0]["premise"]["labels"] == ["base"]
    case.clock[0] = at(1)
    case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"base", "needs-human"}), "open")
    case.reader.get_issue(7)
    assert case.store.github_outbox() == []
    assert case.events == [(7, "remote change")]
    case.reader.flush_outbox()
    assert case.remote.issues[7].labels == frozenset({"base", "needs-human"})
    assert case.remote.issues[7].state == "open" and case.remote.issues[7].title == "Title"


def test_a_stored_write_without_a_premise_is_judged_by_its_recorded_base(case):
    case.store.queue_github_write(7, "add_labels", labels=["mine"])
    snapshot = case.store._issue_snapshot()
    snapshot["outbox_base"] = {"7": snapshot["issues"]["7"]}
    snapshot["outbox"][0]["premise"] = None
    (case.store.root / "issues.json").write_text(json.dumps(snapshot), encoding="utf-8")
    case.clock[0] = at(1)
    case.remote.issues[7] = Issue(7, "Title", "Body", frozenset({"base", "needs-human"}), "open")
    case.reader.get_issue(7)
    assert case.store.github_outbox() == []
    assert case.events == [(7, "remote change")]


# --- issue-state history: what each accepted observation adds, beyond the locked flow tests ---

OPEN_DEP = {"number": 40, "state": "open", "repository": None}


@pytest.fixture
def history(tmp_path):
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])

    def tick():
        clock[0] += timedelta(seconds=1)
        return clock[0]

    return SimpleNamespace(store=store, clock=clock, tick=tick)


def task(number=41, labels=("approved",), state="open"):
    return Issue(number, "Task", "", frozenset(labels), state)


def states(store, number=41):
    return [row["data"] for row in store.flow_records() if row["kind"] == "issue-state" and row["issue"] == number]


def facts(store, number=41):
    return [(data["labels"], data["blocked_by"], data["state"]) for data in states(store, number)]


def forget_history(store):
    """The state before this history existed, or after retention evicted it."""
    for path in store.root.glob("flow-records*.jsonl"):
        path.unlink()


def test_an_edge_only_change_is_a_transition_with_each_edge_state_and_repository(history):
    store, now = history.store, history.tick
    store.refresh_local_issues([task()], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    store.refresh_local_issues([task()], [], {}, now(), blocked_by={41: [{**OPEN_DEP, "state": "closed"}]})
    other = {"number": 40, "state": "closed", "state_reason": "completed", "repository": "other/repo"}
    store.refresh_local_issues([task()], [], {}, now(), blocked_by={41: [
        other, {"number": 40, "state": "closed", "state_reason": "completed"}]})
    assert [data["blocked_by"] for data in states(store)] == [
        [OPEN_DEP],
        [{**OPEN_DEP, "state": "closed"}],
        [{**OPEN_DEP, "state": "closed"}, {"number": 40, "state": "closed", "repository": "other/repo"}],
    ]


def test_edges_not_read_keep_the_last_known_edges_and_never_claim_no_blockers(history):
    store, now = history.store, history.tick
    store.refresh_local_issues([task(labels=["idea"])], [], {}, now(), blocked_by={})
    assert facts(store) == [(["idea"], None, "open")], "Never read is unknown, not an empty list"
    assert store.local_issues()[0]["blocked_by"] == [], "The row keeps its existing value"
    store.refresh_local_issues([task()], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    store.refresh_local_issues([task(labels=["idea"])], [], {}, now(), blocked_by={})
    store.ingest([task(labels=["idea", "x"])], now(), {})
    store.record_labels(41, ["y"], remove=False, lanes={})
    assert [row[1] for row in facts(store)] == [None, [OPEN_DEP], [OPEN_DEP], [OPEN_DEP], [OPEN_DEP]]


def test_a_title_or_state_reason_change_alone_adds_nothing(history):
    store, now = history.store, history.tick
    store.ingest([task()], now(), {})
    store.update_local_issue(41, title="Renamed", state_reason="completed", observed_at=now())
    store.ingest([Issue(41, "Again", "New body", frozenset({"approved"}), "open")], now(), {})
    assert len(states(store)) == 1


def test_labels_are_sorted_and_a_local_label_write_is_recorded_once(history):
    store, now = history.store, history.tick
    store.ingest([task(labels=["z", "a", "m"])], now(), {})
    now()
    store.record_labels(41, ["b"], remove=False, lanes={})
    store.record_labels(41, ["b"], remove=False, lanes={})
    now()
    store.record_labels(41, ["z"], remove=True, lanes={})
    assert [row[0] for row in facts(store)] == [["a", "m", "z"], ["a", "b", "m", "z"], ["a", "b", "m"]]


def test_a_local_edge_only_write_is_recorded_with_the_row_facts(history):
    store, now = history.store, history.tick
    store.ingest([task()], now(), {})
    now()
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    assert facts(store) == [(["approved"], None, "open"), (["approved"], [OPEN_DEP], "open")]
    forget_history(store)
    store.update_local_issue(41, pr_number=9)
    assert states(store) == [], "A field that is not an issue fact is not an observation"


def test_stale_local_writes_add_nothing_and_never_rewrite(history):
    store, now = history.store, history.tick
    late = now()
    store.ingest([task()], now(), {})
    expected = store.flow_records()
    store.update_local_issue(41, labels=["old"], observed_at=late)
    store.update_local_issue(41, labels=["old"], blocked_by=[OPEN_DEP], observed_at=late)
    store.update_local_issue(41, blocked_by=[OPEN_DEP], observed_at=late)
    store.record_labels(41, ["old"], remove=False, lanes={}, observed_at=late)
    store.ingest([task(labels=["old"])], late, {})
    store.refresh_local_issues([task(labels=["old"])], [], {}, late, blocked_by={41: [OPEN_DEP]})
    assert store.flow_records() == expected


def edge_write_at_ten(history, edges=(OPEN_DEP,)):
    """A read at T0+1, then an edge-only write at T0+10: the row's observed_at stays at T0+1."""
    store = history.store
    store.ingest([task()], at(1), {})
    history.clock[0] = at(10)
    store.update_local_issue(41, blocked_by=list(edges))
    return store, store.flow_records()


def test_a_refresh_older_than_an_edge_write_adds_nothing_and_keeps_those_edges(history):
    store, expected = edge_write_at_ten(history)
    store.refresh_local_issues([task(labels=["late"])], [], {}, at(11), observed_at=at(5), blocked_by={41: []})
    assert store.flow_records() == expected
    row = store.local_issues()[0]
    assert row["blocked_by"] == [OPEN_DEP], "The next refresh reuses this list as its read"
    assert row["labels"] == ["late"], "The row's observation rule is unchanged"
    store.refresh_local_issues([task(labels=["late"])], [], {}, at(12), blocked_by={41: row["blocked_by"]})
    assert facts(store)[-1] == (["late"], [OPEN_DEP], "open"), "A newer read carries the labels"
    assert len(states(store)) == len(expected) + 1


def test_a_refresh_tied_with_an_edge_write_it_began_before_keeps_the_write(history):
    store = history.store
    store.ingest([task()], at(1), {})
    revision = store.issues_revision()
    history.clock[0] = at(10)
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    expected = store.flow_records()
    store.refresh_local_issues([task()], [], {}, at(10), since_revision=revision, blocked_by={41: []})
    assert store.flow_records() == expected
    assert store.local_issues()[0]["blocked_by"] == [OPEN_DEP]
    store.refresh_local_issues([task()], [], {}, at(10), since_revision=store.issues_revision(), blocked_by={41: []})
    assert facts(store)[-1][1] == [], "A refresh that began after the write wins the tie, as with facts"


def test_a_refresh_tied_with_an_edge_write_it_began_before_leaves_no_absence(history):
    store = history.store
    store.ingest([task()], at(1), {})
    revision = store.issues_revision()
    history.clock[0] = at(10)
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    expected = store.flow_records()
    store.refresh_local_issues([], [], {}, at(10), since_revision=revision, blocked_by={})
    assert store.flow_records() == expected
    store.refresh_local_issues([], [], {}, at(11), since_revision=store.issues_revision(), blocked_by={})
    assert states(store)[-1]["state"] == "absent", "A later refresh still ends the issue"


def test_a_refresh_that_began_after_a_tied_edge_write_records_its_absence(history):
    store, expected = edge_write_at_ten(history)
    store.refresh_local_issues([], [], {}, at(10), since_revision=store.issues_revision(), blocked_by={})
    assert states(store)[-1]["state"] == "absent"
    assert len(store.flow_records()) == len(expected) + 1


def test_a_tie_needs_no_row_because_the_write_keeps_its_own_revision(history):
    store = history.store
    store.ingest([task()], at(1), {})
    revision = store.issues_revision()
    history.clock[0] = at(10)
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    expected = store.flow_records()
    store.ingest_absence(41, at(5))  # Late: this pops the row, and its revision goes with it.
    assert store.local_issues() == []
    store.refresh_local_issues([], [], {}, at(10), since_revision=revision, blocked_by={})
    assert store.flow_records() == expected


def test_another_write_during_a_refresh_does_not_hold_back_a_tied_edge_write_from_before_it(history):
    store, expected = edge_write_at_ten(history)
    revision = store.issues_revision()  # The refresh begins after the edge write.
    store.update_local_issue(41, pr_number=3)  # This bumps the row, not the edge write.
    store.refresh_local_issues([task()], [], {}, at(10), since_revision=revision, blocked_by={41: []})
    assert store.local_issues()[0]["blocked_by"] == []
    assert facts(store)[-1][1] == [] and len(store.flow_records()) == len(expected) + 1


@pytest.mark.parametrize("read", [
    lambda store: store.ingest([task(labels=["later"])], at(10), {}),
    lambda store: store.ingest_absence(41, at(10)),
    lambda store: store.ingest([], at(10), {}, complete=True),
    lambda store: store.record_labels(41, ["later"], remove=False, lanes={}, observed_at=at(10)),
], ids=["ingest", "absence", "listing", "label"])
def test_outside_a_bulk_refresh_a_tie_lets_the_later_ingest_win_as_observe_does(history, read):
    store, expected = edge_write_at_ten(history)
    read(store)
    assert len(store.flow_records()) == len(expected) + 1


def test_an_edge_write_older_than_an_edge_write_adds_nothing_and_replaces_nothing(history):
    store, expected = edge_write_at_ten(history)
    store.update_local_issue(41, blocked_by=[], observed_at=at(5))
    assert store.flow_records() == expected
    assert store.local_issues()[0]["blocked_by"] == [OPEN_DEP]
    store.update_local_issue(41, blocked_by=[], pr_number=3, observed_at=at(5))
    row = store.local_issues()[0]
    assert (row["blocked_by"], row["pr_number"]) == ([OPEN_DEP], 3), "Other fields stay as they were written"
    assert store.flow_records() == expected


def test_an_unchanged_edge_write_still_moves_the_acceptance_time(history):
    store, expected = edge_write_at_ten(history)
    history.clock[0] = at(20)
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    assert store.flow_records() == expected, "No change, no record"
    store.refresh_local_issues([task()], [], {}, at(21), observed_at=at(15), blocked_by={41: []})
    assert store.flow_records() == expected


@pytest.mark.parametrize("late", [
    lambda store: store.ingest([task(labels=["late"])], at(5), {}),
    lambda store: store.record_labels(41, ["late"], remove=False, lanes={}, observed_at=at(5)),
    lambda store: store.update_local_issue(41, labels=["late"], observed_at=at(5)),
    lambda store: store.update_local_issue(41, labels=["late"], blocked_by=[], observed_at=at(5)),
    lambda store: store.ingest_absence(41, at(5)),
    lambda store: store.ingest([], at(5), {}, complete=True),
    lambda store: store.refresh_local_issues([], [], {}, at(11), observed_at=at(5)),
], ids=["ingest", "label", "update", "update-edges", "absence", "listing", "refresh-omission"])
def test_every_late_read_stays_behind_an_edge_write(history, late):
    store, expected = edge_write_at_ten(history)
    late(store)
    assert store.flow_records() == expected


def test_an_absence_newer_than_an_edge_write_is_recorded_and_the_time_survives_it(history):
    store, expected = edge_write_at_ten(history)
    store.ingest_absence(41, at(5))  # Late: the row goes by the old rule, the history stays.
    store.ingest([task(labels=["late"])], at(6), {})
    assert store.flow_records() == expected, "The time outlives the row"
    store.ingest_absence(41, at(11))
    assert states(store)[-1]["state"] == "absent"
    store.ingest([task()], at(12), {})
    assert states(store)[-1]["state"] == "open"


def test_a_refused_edge_write_does_not_move_the_acceptance_time(history):
    store = history.store
    store.ingest([task()], at(10), {})
    history.clock[0] = at(20)
    store.update_local_issue(41, blocked_by=[OPEN_DEP], observed_at=at(5))
    assert "edges_at" not in store._issue_snapshot()
    store.ingest([task(labels=["new"])], at(11), {})
    assert facts(store)[-1] == (["new"], None, "open")


def test_absence_leaves_one_reason_per_kind_of_evidence_and_a_return_is_recorded(history):
    store, now = history.store, history.tick
    store.refresh_local_issues([task()], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    for _ in range(2):
        store.ingest([], now(), {}, complete=True)
    store.ingest_absence(41, now())
    assert states(store)[1:] == [{"labels": ["approved"], "blocked_by": [OPEN_DEP], "state": "absent",
                                  "reason": "not-listed"}]
    store.ingest([task()], history.clock[0] - timedelta(seconds=5), {})
    assert len(states(store)) == 2, "A read that started before the absence is stale"
    store.ingest([task()], now(), {})
    assert states(store)[2:] == [{"labels": ["approved"], "blocked_by": [OPEN_DEP], "state": "open"}]


def test_a_confirmed_missing_issue_has_its_own_reason_and_uses_the_row_without_history(history):
    store, now = history.store, history.tick
    store.ingest([task(labels=["b", "a"])], now(), {})
    forget_history(store)
    store.ingest_absence(41, now())
    assert states(store) == [{"labels": ["a", "b"], "blocked_by": None, "state": "absent", "reason": "not-found"}]


def test_a_refresh_omission_is_recorded_once_and_an_unknown_absence_adds_nothing(history):
    store, now = history.store, history.tick
    store.ingest([task()], now(), {})
    store.ingest_absence(9, now())  # Comes first in the listing's omissions; must not stop the next.
    for _ in range(2):
        store.refresh_local_issues([], [], {}, now())
    assert [data["state"] for data in states(store)] == ["open", "absent"]
    assert states(store)[-1]["reason"] == "not-listed"
    assert states(store, 9) == [], "No row and no history: nothing to explain"


def test_old_or_evicted_closed_rows_spend_no_history_until_they_change(history):
    store, now = history.store, history.tick
    store.ingest([task(state="closed")], now(), {})
    forget_history(store)
    store.refresh_local_issues([task(state="closed")], [], {}, now(), blocked_by={})
    store.ingest([task(state="closed")], now(), {})
    assert states(store) == []
    store.ingest([task(labels=["approved", "x"], state="closed")], now(), {})
    assert facts(store) == [(["approved", "x"], None, "closed")]


@pytest.mark.parametrize("change", [
    lambda store, at: store.refresh_local_issues([task(labels=["x"], state="closed")], [], {}, at, blocked_by={}),
    lambda store, at: store.ingest([task(labels=["x"], state="closed")], at, {}),
    lambda store, at: store.update_local_issue(41, labels=["x"], observed_at=at),
    lambda store, at: store.record_labels(41, ["x"], remove=False, lanes={}, observed_at=at),
], ids=["refresh", "ingest", "update", "label"])
def test_each_writer_sees_an_old_closed_row_as_it_stood_before_the_write(history, change):
    store, now = history.store, history.tick
    store.ingest([task(labels=[], state="closed")], now(), {})
    forget_history(store)
    change(store, now())
    assert [row[0] for row in facts(store)] == [["x"]]


def test_an_explicit_edge_change_on_an_old_closed_row_is_recorded(history):
    store, now = history.store, history.tick
    store.refresh_local_issues([task(state="closed")], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    forget_history(store)
    store.refresh_local_issues([task(state="closed")], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    assert states(store) == []
    store.update_local_issue(41, blocked_by=[])
    assert facts(store) == [(["approved"], [], "closed")]


def test_an_old_open_row_without_history_gets_its_first_record(history):
    store, now = history.store, history.tick
    store.ingest([task()], now(), {})
    forget_history(store)
    store.ingest([task()], now(), {})
    assert facts(store) == [(["approved"], None, "open")]


def test_queued_intents_and_placeholders_wait_for_an_accepted_observation(history):
    store, now = history.store, history.tick
    store.queue_github_write(41, "add_labels", labels=["mine"], lanes={})
    store.update_local_issue(41, blocked_by=[OPEN_DEP])
    store.update_local_issue(42, pr_number=3)
    assert store.flow_records() == []
    store.ingest_absence(42, now())
    assert states(store, 42) == [], "A placeholder never observed has no history to end"
    store.ingest([task(labels=["mine"])], now(), {})
    assert facts(store) == [(["mine"], None, "open")]


def test_pull_request_rows_have_no_issue_history(history):
    store, now = history.store, history.tick
    store.ingest([Issue(41, "PR", "", frozenset({"x"}), "open", is_pull_request=True)], now(), {})
    store.ingest([task()], now(), {})
    forget_history(store)
    store.update_local_issue(41, is_pull_request=True)
    store.update_local_issue(41, labels=["y"], blocked_by=[OPEN_DEP], observed_at=now())
    store.ingest_absence(41, now())
    assert states(store) == []


def test_a_failed_record_changes_no_result_and_the_next_observation_repairs_it(history, monkeypatch):
    store, now = history.store, history.tick

    def broken(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "flow_records", broken)
        store.ingest([task()], now(), {})
        store.update_local_issue(41, blocked_by=[OPEN_DEP])
        store.record_labels(41, ["a"], remove=False, lanes={})
        assert store.refresh_local_issues([task(labels=["a"])], [], {}, now()) == []
        assert store.ingest_absence(77, now()) is False
    assert store.flow_records() == []
    assert store.local_issues()[0]["labels"] == ["a"]
    store.ingest([task(labels=["a"])], now(), {})
    assert facts(store) == [(["a"], None, "open")]


def test_a_write_that_cannot_be_saved_leaves_no_history(history, monkeypatch):
    store, now = history.store, history.tick

    def refused(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_json", refused)
    with pytest.raises(OSError):
        store.ingest([task()], now(), {})
    with pytest.raises(OSError):
        store.record_labels(41, ["a"], remove=False, lanes={})
    assert store.flow_records() == []


def test_one_refresh_reads_history_once_and_a_stale_observation_reads_none(history, monkeypatch):
    store, now = history.store, history.tick
    late = now()
    store.refresh_local_issues([task(), task(42)], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    reads = []
    original = StateStore.flow_records
    monkeypatch.setattr(StateStore, "flow_records", lambda self, **kw: reads.append(1) or original(self, **kw))
    store.ingest([task()], late, {})
    assert reads == []
    store.refresh_local_issues([task(), task(42)], [], {}, now(), blocked_by={41: [OPEN_DEP]})
    assert reads == [1]
    assert (len(states(store)), len(states(store, 42))) == (1, 1), "One refresh gives one record, edges included"


def test_concurrent_writers_lose_no_record_and_never_repeat_one(tmp_path):
    root, clock = tmp_path / "state", [T0 + timedelta(seconds=1)]
    start = threading.Barrier(8)

    def writer(number):
        store = StateStore(root, now=lambda: clock[0])
        start.wait()
        for _ in range(3):
            store.ingest([task(number)], clock[0], {})
            store.record_labels(number, ["mine"], remove=False, lanes={})

    threads = [threading.Thread(target=writer, args=(number,)) for number in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    store = StateStore(root, now=lambda: clock[0])
    for number in range(1, 9):
        assert [data["labels"] for data in states(store, number)] == [["approved"], ["approved", "mine"]] * 3

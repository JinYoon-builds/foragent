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

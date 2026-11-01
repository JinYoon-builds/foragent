"""The person boundary on the GitHub intent log: observed owner edits and known PR targets (#1004)."""

import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent.config import Config  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, RecordingTracker  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def queue_case(tmp_path, *issues):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(legacy_owner="alice"))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, NOW)
    return store, tracker, recorder


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def visible(store, number):
    return next(row for row in store.local_issues() if row["number"] == number)


def relabel(tracker, number, old, new):
    tracker.remove_labels(number, [old])
    tracker.add_labels(number, [new])


@pytest.mark.parametrize("action, payload", [
    ("comment", {"body": "Mine once"}), ("add_labels", {"labels": ["ready"]}), ("close_issue", {"body": None})])
@pytest.mark.parametrize("read", ["bulk", "single"])
def test_an_observed_owner_edit_refuses_the_write_inspectably(tmp_path, action, payload, read):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(1, action, lanes={}, **payload)
    store.queue_github_write(2, "comment", body="Still mine")
    relabel(tracker, 1, "by:alice", "by:bob")

    if read == "bulk":
        store.refresh_local_issues([tracker.get_issue(1), tracker.get_issue(2)], [], {}, NOW)
    else:
        recorder.get_issue(1)

    [refused] = store.refused_github_writes()
    assert refused["issue"] == 1 and refused["action"] == action
    assert "by:alice" in refused["refused"] and "by:bob" in refused["refused"]
    assert [entry["issue"] for entry in store.github_outbox()] == [2]
    assert visible(store, 1)["labels"] == ["by:bob"] and visible(store, 1)["state"] == "open"
    assert [(e["kind"], e["issue"], e["action"], e["owner"]) for e in events(store)] == [
        ("ownership-refused", 1, action, "bob")]

    recorder.flush_outbox()
    assert tracker.list_comments(1) == [] and tracker.get_issue(1).state == "open"
    assert [c.body for c in tracker.list_comments(2)] == ["Still mine"]


def test_a_persons_other_label_edit_still_drops_the_write(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "comment", body="Stale")
    tracker.add_labels(1, ["blocked"])

    recorder.get_issue(1)

    assert store.github_outbox() == [] and store.refused_github_writes() == []
    assert [e["kind"] for e in events(store)] == ["outbox-dropped"]


def test_refusal_keeps_a_sent_write_and_removes_only_the_unsent_effect(tmp_path):
    store, _, _ = queue_case(tmp_path, (1, ["by:alice"]))
    sent = store.queue_github_write(1, "add_labels", lanes={}, labels=["sent"])
    store.record_github_attempt(sent["id"], NOW, True)
    store.queue_github_write(1, "add_labels", lanes={}, labels=["unsent"])
    assert visible(store, 1)["labels"] == ["by:alice", "sent", "unsent"]

    assert store.refuse_github_writes(1, "not yours") == 1

    assert [entry["labels"] for entry in store.refused_github_writes()] == [["unsent"]]
    assert visible(store, 1)["labels"] == ["by:alice", "sent"]  # Awaits the read that shows it.
    assert store.github_write_issues() == {1}


@pytest.mark.parametrize("read", ["listing", "bulk"])
def test_a_complete_listing_does_not_call_a_known_pr_missing(tmp_path, read):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.update_local_issue(1, pr_number=7)
    store.queue_github_write(7, "comment", body="On the PR")

    if read == "listing":
        recorder.list_issues("all")
    else:
        store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)

    assert [entry["issue"] for entry in store.github_outbox()] == [7]
    assert [e["kind"] for e in events(store)] == []


@pytest.mark.parametrize("entry", ["run_issue", "review_only"])
def test_a_refused_entry_takes_no_machine_slot(git_history, tmp_path, monkeypatch, entry):
    from foragent import capacity, runner
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, ["by:bob"])
    taken = []
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **k: taken.append(k) or (_ for _ in ()).throw(
        AssertionError("a refused entry must not wait for a machine slot")))

    assert getattr(runner, entry)(1, case.ctx) == "refused"
    assert taken == []


def test_evidence_outlives_an_owner_edit_until_replay_refuses_it(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "comment", body="Accepted advice", evidence=True)
    relabel(tracker, 1, "by:alice", "by:bob")

    recorder.get_issue(1)
    assert [entry["body"] for entry in store.github_outbox()] == ["Accepted advice"]

    recorder.flush_outbox()
    assert store.github_outbox() == [] and tracker.list_comments(1) == []
    assert [entry["body"] for entry in store.refused_github_writes()] == ["Accepted advice"]

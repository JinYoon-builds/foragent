"""Shared blocker readers use observed labels and marked comments, never old flags."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch, orch_push
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import Issue

NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)


def comment(identity, reason, *, at=NOW, label="blocked"):
    return {"id": identity, "author": "bot", "created_at": at.isoformat(),
            "body": f"<!-- foragent:{label} reason={reason} -->\r\nDetails {identity}"}


@pytest.fixture
def case(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    issue = Issue(1, "Choose a locale", "", frozenset({"blocked"}), "open")
    store.ingest([issue], NOW, {})
    store.save_flag(1, "hash-mismatch", flagged_at=(NOW - timedelta(days=1)).isoformat())
    return SimpleNamespace(store=store, issue=issue, config=Config(), now=lambda: NOW)


def test_current_reason_time_and_details_agree_across_readers_after_restart(case):
    newest = comment(3, "future-reason", at=NOW + timedelta(minutes=1))
    case.store.record_comments(1, [newest, comment(2, "stalled"), comment(1, "hash-mismatch")])
    case.store = StateStore(case.store.root)
    flag = case.store.load_flag(1)
    assert (flag.reason, flag.flagged_at) == ("future-reason", newest["created_at"])
    row, = orch.list_blocked(case)
    assert (row["reason"], row["blocked_at"], row["details"]) == (
        flag.reason, flag.flagged_at, newest["body"])
    assert orch_push.blocked_now(case.store) == {1: ("blocked", "future-reason")}


def test_marker_tie_uses_id_and_ignores_discussion_and_absent_label(case):
    wrong_label = comment(20, "timeout", label="needs-human", at=NOW + timedelta(hours=1))
    discussion = {**comment(30, "timeout"), "body": "Discussion\n" + comment(30, "timeout")["body"]}
    case.store.record_comments(1, [comment(1, "stalled"), comment(3, "spec-question"),
                                 discussion, wrong_label])
    assert case.store.load_flag(1).reason == "spec-question"


@pytest.mark.parametrize("old_reason", ["hash-mismatch", "future-reason"])
@pytest.mark.parametrize("state,labels", [("open", {"approved"}), ("closed", {"blocked"})])
def test_unblock_or_close_hides_history_and_suppresses_notification(case, monkeypatch, state, labels, old_reason):
    case.store.save_flag(1, old_reason)
    case.store.record_comments(1, [comment(1, "stalled")])
    # The later observation wins even when an old read finishes afterwards (#1125).
    case.store.ingest([Issue(1, "Choose a locale", "", frozenset(labels), state)], NOW + timedelta(seconds=2), {})
    case.store.ingest([case.issue], NOW + timedelta(seconds=1), {})
    assert StateStore(case.store.root).load_flag(1) is None
    assert orch.list_blocked(case) == []
    monkeypatch.setattr(orch_push, "_orch_off", lambda *args: (False, True))
    monkeypatch.setattr("foragent.notify.send", lambda *args: pytest.fail("Historical blocker was pushed"))
    assert orch_push.after_tick(case, spawned=False) == ([], False)


def test_absent_issue_cannot_resurrect_a_historical_flag(case):
    case.store.ingest_absence(1, NOW + timedelta(seconds=1))
    assert StateStore(case.store.root).load_flag(1) is None


def test_unmarked_blockage_does_not_inherit_an_old_reason(case):
    assert case.store.load_flag(1) is None
    row, = orch.list_blocked(case)
    assert (row["reason"], row["blocked_at"], row["details"]) == (None, None, "")
    assert orch_push.blocked_now(case.store) == {1: ("blocked", "")}


def test_pending_marker_uses_fifo_and_is_retired_by_unblock(case):
    case.store.record_comments(1, [comment(1, "stalled")])
    case.store.queue_github_write(1, "comment", body=comment(2, "spec-question")["body"])
    case.store.queue_github_write(1, "comment", body=comment(3, "review-dispute")["body"])
    flag = case.store.load_flag(1)
    assert (flag.reason, flag.flagged_at) == ("review-dispute", NOW.isoformat())
    row, = orch.list_blocked(case)
    assert row["reason"] == flag.reason and "Details 3" in row["details"]
    assert orch_push.blocked_now(case.store) == {1: ("blocked", "review-dispute")}
    case.store.ingest([Issue(1, "Choose a locale", "", frozenset(), "open")], NOW + timedelta(seconds=1), {})
    assert case.store.github_outbox() == []
    assert case.store.load_flag(1) is None


def test_pending_unblock_masks_delivered_blocker(case):
    case.store.record_comments(1, [comment(1, "stalled")])
    case.store.queue_github_write(1, "remove_labels", labels=["blocked"])
    assert case.store.load_flag(1) is None
    assert orch.list_blocked(case) == []
    assert orch_push.blocked_now(case.store) == {}


def test_human_marker_is_current_only_while_human_label_is_present(case):
    case.store.record_labels(1, ["needs-human"], remove=False, lanes={})
    case.store.record_comments(1, [comment(1, "stalled"), comment(2, "spec-question", label="needs-human")])
    assert case.store.load_flag(1).reason == "spec-question"
    case.store.record_labels(1, ["blocked"], remove=True, lanes={})
    assert case.store.load_flag(1).reason == "spec-question"
    assert orch_push.blocked_now(case.store) == {1: ("needs-human", "spec-question")}
    case.store.record_labels(1, ["needs-human"], remove=True, lanes={})
    assert case.store.load_flag(1) is None


@pytest.mark.parametrize("label", ["blocked", "needs-human"])
@pytest.mark.parametrize("pending", [False, True], ids=["delivered", "pending"])
def test_pull_request_cache_row_cannot_be_a_shared_blocker(case, label, pending):
    case.store.update_local_issue(1, labels=[label], is_pull_request=True)
    marked = comment(1, "spec-question", label=label)
    if pending:
        case.store.queue_github_write(1, "comment", body=marked["body"])
    else:
        case.store.record_comments(1, [marked])
    assert case.store.local_issues()[0]["is_pull_request"] is True

    assert StateStore(case.store.root).load_flag(1) is None
    assert orch.list_blocked(case) == []


def test_machine_only_flag_keeps_its_local_contract(case):
    case.store.save_flag(1, "parent-child-not-planned", local_only=True)
    assert StateStore(case.store.root).load_local_flag(1).reason == "parent-child-not-planned"
    assert case.store.load_flag(1).local_only is True
    case.store.save_flag(1, "spec-question")
    assert case.store.load_local_flag(1).local_only is False
    assert case.store.load_flag(1) is None


def test_parent_local_notice_is_deduplicated_without_masking_a_shared_blocker(case, monkeypatch):
    from foragent import board, board_parents

    cancelled = Issue(2, "Cancelled child", "", frozenset(), "closed", state_reason="not_planned")
    unfinished = Issue(3, "Open child", "", frozenset(), "open")
    case.store.refresh_local_issues([case.issue, cancelled, unfinished], [], {}, NOW,
                                   children={1: [2, 3]}, comments={1: [comment(1, "spec-question")]})
    case.tracker = SimpleNamespace(sub_issues=lambda n: [cancelled, unfinished])
    report, events = SimpleNamespace(actions=[]), []
    monkeypatch.setattr(board, "sync_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(board_parents, "append_event", lambda *args: events.append(args))
    board_parents.follow_children(case, report)
    board_parents.follow_children(case, report)
    assert len(events) == 1
    assert case.store.load_local_flag(1).reason == board_parents.NOT_PLANNED_FLAG
    assert case.store.load_flag(1).reason == "spec-question"

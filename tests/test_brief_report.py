"""Unit tests for `fa brief --report` (brief_report)."""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import brief_report
from foragent.orch_events import append_event
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def store_at(tmp_path, clock):
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.root.mkdir(parents=True)
    return store


def merge(store, clock, number, at):
    clock[0] = at - timedelta(minutes=10)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane="api")
    clock[0] = at
    store.record_merge(number, at)
    store.finish_run(number, outcome="merged")


def test_without_a_note_the_first_line_counts_and_a_hold_resolved_by_merge_is_reported(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=30), 7, "escalate", "#7: escalated to human")
    merge(store, clock, 7, NOW - timedelta(hours=1))
    report = brief_report.collect(store, NOW)
    assert report["judgment"] is None
    assert report["merged"] == [{"issue": 7, "title": None}]
    assert report["resolved"] == [{"issue": 7, "via": "merged", "line": None}]
    lines = brief_report.render(report, "en").splitlines()
    assert lines[0] == "0 working · 0 waiting for you · 1 merged since the last report"
    assert "- Hold resolved #7: merged after the hold" in lines
    assert lines[-1] == "Nothing needs you right now."


@pytest.mark.parametrize("kind", ["escalate", "retries-exceeded", "orchestrator-timeout", "stalled"])
@pytest.mark.parametrize("back", ["retry", "crash-restart", "stall-restart", "resolve"])
def test_every_recorded_hold_and_return_to_ready_is_paired(tmp_path, kind, back):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=3), 5, kind, f"#5 {kind}")
    fields = {"requeued": True} if back == "resolve" else {}
    append_event(store, NOW - timedelta(hours=1), 5, back, f"#5 {back}", say="Back in the queue.", **fields)
    assert brief_report.collect(store, NOW)["resolved"] == [{"issue": 5, "via": "event", "line": "Back in the queue."}]


def test_a_resolve_that_did_not_requeue_does_not_end_a_hold(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=3), 5, "escalate", "#5 escalated")
    append_event(store, NOW - timedelta(hours=1), 5, "resolve", "#5 resolved; merge-blocked", requeued=False)
    assert brief_report.collect(store, NOW)["resolved"] == []


def test_a_flagged_issue_approved_again_is_a_resolved_hold(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    store.save_flag(6, "retries-exceeded", flagged_at=(NOW - timedelta(hours=5)).isoformat())
    store.save_approval(6, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=(NOW - timedelta(hours=26)).isoformat())
    assert brief_report.collect(store, NOW)["resolved"] == []  # The approval predates the hold.
    store.save_approval(6, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=(NOW - timedelta(hours=2)).isoformat())
    report = brief_report.collect(store, NOW)
    assert report["resolved"] == [{"issue": 6, "via": "approved", "line": None}]
    assert "- Hold resolved #6: approved again after the hold" in brief_report.render(report, "en").splitlines()


def test_a_hold_that_started_after_its_retry_is_not_resolved(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=2), 8, "retry", "#8: queued implementation retry")
    append_event(store, NOW - timedelta(hours=1), 8, "escalate", "#8: escalated to human")
    assert brief_report.collect(store, NOW)["resolved"] == []


def test_the_window_starts_at_the_recorded_report_time(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    merge(store, clock, 1, NOW - timedelta(hours=2))
    merge(store, clock, 2, NOW - timedelta(minutes=30))
    assert brief_report.last_report(store) is None
    brief_report.record_report(store, NOW - timedelta(hours=1))
    assert brief_report.last_report(store) == NOW - timedelta(hours=1)
    assert [item["issue"] for item in brief_report.collect(store, NOW)["merged"]] == [2]


def test_questions_show_every_choice_and_mark_the_recommended_one():
    report = {"judgment": "Decide the region.", "merged": [], "resolved": [], "working": 2, "lanes": 1,
              "now": None, "questions": [{"question": "Region?", "options": ["Seoul", "Tokyo"], "recommend": 2}],
              "person_issues": [{"issue": 9, "title": "Policy"}]}
    lines = brief_report.render(report, "en").splitlines()
    assert lines == ["Decide the region.", "Nothing finished since the last report.",
                     "Now: 2 working · lanes busy: 1", "Your turn:",
                     "- Region? 1. Seoul / 2. Tokyo (recommended)", "- #9 Policy needs your decision"]


def test_welcome_draws_the_big_squirrel_with_or_without_colour():
    plain = brief_report.welcome("ko", colour=False)
    assert "\x1b[" not in plain and "█" in plain
    assert plain.splitlines()[-1].startswith("안녕하세요, Foragent예요.")
    assert len(plain.splitlines()) == 10  # Eight cell rows, a blank line and the greeting.
    assert "\x1b[38;5;" in brief_report.welcome("en", colour=True)

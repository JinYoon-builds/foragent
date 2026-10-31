"""Whose turn each question awaits (#1160): the shared projection's edge cases."""

from datetime import UTC, datetime, timedelta
import os

from foragent import orch_inbox, watch_questions
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def _store(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def _spec_question(store, tracker, number, clock):
    tracker.add_issue(number, f"Issue {number}", labels=["approved", "blocked", "lane:api"])
    store.start_run(number, branch=f"issue-{number}", worktree="unused", lane="api")
    store.save_flag(number, "spec-question", flagged_at=clock[0].isoformat())
    store.finish_run(number, outcome="spec-question", detail="Which region?")


def _refresh(store, tracker, clock):
    store.refresh_local_issues(tracker.list_issues("all"), [], {"api": {}}, clock[0])


def test_a_spec_question_flagged_again_after_the_answer_is_the_orchestrators(tmp_path):
    """Defect class: an old answer hiding a newer spec question on the same issue."""
    clock = [NOW]
    store, tracker = _store(tmp_path, clock), FakeTracker(now=lambda: clock[0])
    _spec_question(store, tracker, 7, clock)
    asked = orch_inbox.ask(store, "Which region?", ["Seoul", "Tokyo"], 1, issues=[7])
    clock[0] += timedelta(minutes=1)
    orch_inbox.answer(store, asked["id"], "Seoul")
    _refresh(store, tracker, clock)
    assert watch_questions.recipients(store) == {7: watch_questions.ANSWERED}
    # A later run asks again: the flag and its file are newer than the answer.
    clock[0] += timedelta(minutes=5)
    store.start_run(7, branch="issue-7", worktree="unused", lane="api")
    store.save_flag(7, "spec-question", flagged_at=clock[0].isoformat())
    store.finish_run(7, outcome="spec-question", detail="Which zone?")
    later = clock[0].timestamp() + 1
    os.utime(store.root / "flags" / "7.json", (later, later))
    assert watch_questions.recipients(store) == {7: watch_questions.ORCHESTRATOR}
    assert watch_questions.waiting(watch_questions.recipients(store)) == {"person": 0, "orchestrator": 1}


def test_parked_work_and_a_moved_on_run_are_no_questions(tmp_path):
    """Defect class: treating parked work, or a flag whose run moved on, as an open question."""
    clock = [NOW]
    store, tracker = _store(tmp_path, clock), FakeTracker(now=lambda: clock[0])
    tracker.add_issue(4, "Parked", labels=["approved", "waiting-person", "lane:api"])
    tracker.add_issue(5, "Asked", labels=["approved", "needs-human", "lane:api"])
    _spec_question(store, tracker, 6, clock)
    store.start_run(6, branch="issue-6", worktree="unused", lane="api")  # The retry.
    _refresh(store, tracker, clock)
    assert watch_questions.recipients(store) == {5: watch_questions.PERSON}


def _frame(store, tracker, clock, language="en"):
    from foragent import status, watch
    from foragent.config import Config

    config = Config(language=language, view_glyphs="ascii", lanes={"api": {}})
    snap = watch.collect(status.collect(store, config, clock[0]), tracker, store, config, clock[0])
    return watch.render_frame(snap, config, store.root, width=120, now=clock[0]).splitlines()


def test_an_answer_wins_over_a_needs_human_label_until_it_is_routed_again(tmp_path):
    """Defect class: stale routing (a label left in place) overriding the recorded answer."""
    from foragent import presence

    clock = [NOW]
    store, tracker = _store(tmp_path, clock), FakeTracker(now=lambda: clock[0])
    tracker.add_issue(8, "Pick a plan", labels=["approved", "needs-human", "lane:api"])
    marker = {"id": 1, "author": "foragent", "body": "<!-- foragent:needs-human reason=escalated -->\nPick one.",
              "created_at": NOW.isoformat()}
    asked = orch_inbox.ask(store, "Plan A or B?", ["A", "B"], 1, issues=[8])
    store.refresh_local_issues(tracker.list_issues("all"), [], {"api": {}}, clock[0], comments={8: [marker]})
    row = next(line for line in _frame(store, tracker, clock) if "#8" in line)
    assert "waiting for you" in row and presence.counts(store, clock[0])[1] == 1
    orch_inbox.answer(store, asked["id"], "A")  # Answered at NOW, after the marker.
    found = watch_questions.collect(store)
    assert found.issues == {8: watch_questions.ANSWERED}
    frame = _frame(store, tracker, clock)
    row = next(line for line in frame if "#8" in line)
    assert "waiting to restart" in row and "you" not in row, row
    assert "question" not in frame[0], frame[0]
    assert presence._question_groups(store, "en") == "" and presence.counts(store, clock[0])[1] == 0
    # Routed to the person again, later than the answer: the person's turn once more.
    again = {**marker, "id": 2, "created_at": (NOW + timedelta(hours=1)).isoformat()}
    store.refresh_local_issues(tracker.list_issues("all"), [], {"api": {}}, clock[0],
                               comments={8: [marker, again]})
    assert watch_questions.recipients(store) == {8: watch_questions.PERSON}
    assert presence._question_groups(store, "en") == " · questions: you 1"


def test_an_open_question_without_an_issue_is_the_persons(tmp_path):
    """Defect class: questions lost because the projection only keeps issue-linked ones."""
    from foragent import presence

    clock = [NOW]
    store, tracker = _store(tmp_path, clock), FakeTracker(now=lambda: clock[0])
    tracker.add_issue(9, "Busy", labels=["approved", "lane:api"])
    _refresh(store, tracker, clock)
    asked = orch_inbox.ask(store, "Keep the weekly report?", ["yes", "no"], 1)
    assert watch_questions.collect(store) == watch_questions.Questions({}, 1)
    assert presence._question_groups(store, "ko") == " · 질문: 진님 1"
    assert "답할 질문 1개" in _frame(store, tracker, clock, "ko")[0]
    orch_inbox.answer(store, asked["id"], "yes")
    assert presence._question_groups(store, "ko") == ""
    assert "답할 질문" not in _frame(store, tracker, clock, "ko")[0]


def test_a_question_names_its_recipient_whatever_the_work_state(tmp_path):
    """Defect class: question facts discarded because of the work state (queued, folded, unlisted)."""
    from foragent import presence

    clock = [NOW]
    store, tracker = _store(tmp_path, clock), FakeTracker(now=lambda: clock[0])
    tracker.add_issue(10, "Next in line", labels=["approved", "lane:api"])
    tracker.add_issue(11, "Queued and asked", labels=["approved", "lane:api"])
    tracker.add_issue(12, "Not approved, asked", labels=["lane:api"])
    tracker.add_issue(13, "Queued, quiet", labels=["approved", "lane:api"])
    _refresh(store, tracker, clock)
    for number in (11, 12):
        orch_inbox.ask(store, f"About #{number}?", ["a", "b"], 1, issues=[number])
    frame = _frame(store, tracker, clock)  # No view state: the lane is folded.
    rows = {number: next((line for line in frame if f"#{number} " in line), None) for number in (10, 11, 12, 13)}
    assert "waiting for a turn" in rows[10], frame
    assert rows[11] is not None and "waiting for you" in rows[11], frame
    assert rows[12] is not None and "waiting for you" in rows[12], frame
    assert rows[13] is None, "a folded lane still hides quiet queued work"
    assert presence._question_groups(store, "en") == " · questions: you 2"

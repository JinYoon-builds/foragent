"""Boundaries for one-line transition comments (#1186)."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from test_github_outbox import github_outbox_case, outbox_rows, persisted_store  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401

from foragent.config import Config  # noqa: E402
from foragent.reasons import Reason, comment_order, flag_text, format_comment, one_line  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, Issue  # noqa: E402

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_repeating_a_retry_while_writes_wait_for_github_queues_one_comment(git_history, tmp_path, monkeypatch):
    """GitHub still shows blocked; the pending local intent already says ready."""
    from foragent import orch

    case = github_outbox_case(git_history, tmp_path, monkeypatch, action="add_labels")
    orch.retry(case.ctx, 1, "Use the selected locale")
    first = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(first) == 1, "The first retry is a change and queues its comment"
    orch.retry(case.ctx, 1, "Use the selected locale")
    rows = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert rows == first, "The same retry again changes nothing that waits for GitHub"
    orch.retry(case.ctx, 1, "Use the English locale")
    rows = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(rows) == 2 and "English" in rows[-1]["body"], "A new hint is a change"


def test_queued_comments_with_one_time_order_by_queue_id_after_delivered_ones():
    at = NOW.isoformat()
    comments = [{"id": -2, "created_at": at}, {"id": 7, "created_at": at}, {"id": -1, "created_at": at}]
    assert [c["id"] for c in sorted(comments, key=comment_order)] == [7, -1, -2]


def test_flag_after_queued_flags_with_one_time_compares_with_the_newest(tmp_path):
    from foragent.actions import flag

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(1, "Issue", "body", labels=["blocked"])
    store.record_issue(Issue(1, "Issue", "body", frozenset({"blocked"}), "open"), {})
    a = format_comment(Reason.TIMEOUT, "A", label="blocked")
    b = format_comment(Reason.TIMEOUT, "B", label="blocked")
    store.save_flag(1, Reason.TIMEOUT, summary="B")
    for body in (b, a):  # The comment text disagrees with the observed reason on purpose.
        store.queue_github_write(1, "comment", body=body)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW, config=Config(), repo="owner/repo")
    flag(ctx, 1, Reason.TIMEOUT, "B")
    assert tracker.list_comments(1) == [], "The observed reason is B, regardless of the newest comment text"
    flag(ctx, 1, Reason.TIMEOUT, "A")
    comments = [row["body"] for row in store.github_outbox() if row["action"] == "comment"]
    assert comments == [b, a, a], "A after observed reason B is a change, even if an old comment says A"
    assert tracker.list_comments(1) == [], "Delivery still waits for the existing outbox"


@pytest.mark.parametrize("details", [
    'printf "a  b"', "  indented\n\n\ttab  and trailing  ", "line one\r\nline two\n",
])
def test_one_line_evidence_gives_back_the_exact_lines(details):
    body = format_comment(Reason.SPEC_QUESTION, "Choose", details, label="blocked")
    assert len(body.splitlines()) == 1
    assert flag_text(body) == ("Choose", "\n".join(details.splitlines()))


def test_one_line_keeps_spacing_in_orch_summaries():
    assert one_line("<!-- foragent:orch -->", "#1: hint: run  `a  b`\nnext") == (
        "<!-- foragent:orch --> #1: hint: run  `a  b` ↵ next")


def _new_comments(case, before):
    return [c.body for c in case.tracker.list_comments(1)[before:]]


def test_retry_record_keeps_the_exact_hint_so_different_commands_differ(git_history, tmp_path, monkeypatch):
    from foragent import orch
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    before = len(case.tracker.list_comments(1))
    orch.retry(case.ctx, 1, 'printf "a  b"')
    orch.retry(case.ctx, 1, 'printf "a b"')
    added = _new_comments(case, before)
    assert len(added) == 2 and all(len(body.splitlines()) == 1 for body in added)
    assert 'printf "a  b"' in added[0] and 'printf "a b"' in added[1]


def test_repeating_an_unchanged_escalation_adds_neither_comment(git_history, tmp_path, monkeypatch):
    from foragent import orch
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    before = len(case.tracker.list_comments(1))
    args = dict(summary="Choose a locale", tried="Reviewed  twice", options=["English", "Korean"], recommend=1)
    orch.escalate(case.ctx, 1, **args)
    first = _new_comments(case, before)
    assert len(first) == 2 and all(len(body.splitlines()) == 1 for body in first)
    assert "Reviewed  twice" in first[0] and "Reviewed  twice" in first[1], "Both keep the exact text"
    orch.escalate(case.ctx, 1, **args)
    assert _new_comments(case, before) == first, "The same escalation again is no new fact"
    orch.escalate(case.ctx, 1, **{**args, "tried": "Reviewed three times"})
    assert len(_new_comments(case, before)) == 4, "A changed escalation is a new fact"


def test_parking_again_for_the_same_reason_adds_no_record(git_history, tmp_path, monkeypatch):
    from foragent import orch_park
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    before = len(case.tracker.list_comments(1))
    orch_park.park(case.ctx, 1, "wait for  the design")
    orch_park.park(case.ctx, 1, "wait for  the design")
    added = _new_comments(case, before)
    assert len(added) == 1 and "wait for  the design" in added[0]
    orch_park.park(case.ctx, 1, "wait for the design")
    assert len(_new_comments(case, before)) == 1, "A new park explanation alone is no issue-state change"


@pytest.mark.parametrize("text", ["echo a ↵ echo b", "a ↵\nb", "a\n↵b", "x↵ y", " ↵ ", "↵↵\n↵"])
def test_a_literal_separator_reads_back_exactly_and_never_looks_like_a_line_break(text):
    from foragent.reasons import decode_line

    line = one_line("<!-- foragent:orch -->", text)
    assert len(line.splitlines()) == 1
    assert decode_line(line.removeprefix("<!-- foragent:orch --> ")) == text
    if (as_lines := text.replace(" ↵ ", "\n")) != text:
        assert line != one_line("<!-- foragent:orch -->", as_lines), "A literal separator is not a line break"


def test_flag_evidence_changed_from_lines_to_a_literal_separator_is_a_new_comment(tmp_path):
    from dataclasses import asdict

    from foragent.actions import flag

    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(1, "Issue", "body", labels=["running"])
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW, config=Config(), repo="owner/repo")
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out", "echo a\necho b")
    store.record_issue(tracker.get_issue(1), {})
    store.record_comments(1, [asdict(c) for c in tracker.list_comments(1)])
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out ↵ again", "echo a ↵ echo b")
    bodies = [c.body for c in tracker.list_comments(1)]
    assert len(bodies) == 2, "A different recorded reason summary is a state change"
    assert [flag_text(body)[1] for body in bodies] == ["echo a\necho b", "echo a ↵ echo b"]


def test_escalation_and_park_texts_with_a_literal_separator_are_distinct_facts(git_history, tmp_path, monkeypatch):
    from foragent import orch, orch_park
    from foragent.reasons import decode_line
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    before = len(case.tracker.list_comments(1))
    orch.escalate(case.ctx, 1, summary="Choose", tried="step one\nstep two")
    orch.escalate(case.ctx, 1, summary="Choose", tried="step one ↵ step two")
    added = _new_comments(case, before)
    assert len(added) == 4, "The second escalation's text differs, so both comments are new"
    assert "step one ↵ step two" in decode_line(added[2].partition("--> ")[2])
    orch_park.park(case.ctx, 1, "wait\nfor design")
    orch_park.park(case.ctx, 1, "wait ↵ for design")
    assert len(_new_comments(case, before)) == 5, "Only the first park changes labels; explanations do not define state"


@pytest.mark.parametrize("label", ["blocked", "needs-human", "approved"])
def test_repark_records_every_label_it_removes_even_with_the_same_reason(
    git_history, tmp_path, monkeypatch, label,
):
    from foragent import orch_park
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    orch_park.park(case.ctx, 1, "wait")
    before = len(case.tracker.list_comments(1))
    case.tracker.record_label(1, label, "human")
    orch_park.park(case.ctx, 1, "wait")
    assert label not in case.tracker.get_issue(1).labels
    assert len(_new_comments(case, before)) == 1, "Removing a restored label is a new transition"
    orch_park.park(case.ctx, 1, "wait")
    assert len(_new_comments(case, before)) == 1, "No second transition, no second comment"


def test_reescalate_records_removing_ready_even_when_needs_human_is_already_present(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    before = len(case.tracker.list_comments(1))
    case.tracker.add_labels(1, ["ready"])
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    assert "ready" not in case.tracker.get_issue(1).labels
    assert len(_new_comments(case, before)) == 2, "The escalation line and its orch record accompany the change"
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    assert len(_new_comments(case, before)) == 2


def _flag_context(tmp_path, labels=("running",)):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(1, "Issue", "body", labels=list(labels))
    store.record_issue(tracker.get_issue(1), {})
    return SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW, config=Config(), repo="owner/repo")


def test_unchanged_runner_flag_ignores_comment_history_and_does_not_refresh_its_clock(tmp_path, monkeypatch):
    from foragent.actions import flag

    ctx = _flag_context(tmp_path)
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out", "Full evidence")
    first = ctx.tracker.list_comments(1)
    flag_record = ctx.store.load_flag(1)
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.store.record_comments(1, [])  # Missing/old/different comments are never comparison input.
    from foragent.orch_transition import transition_state
    with monkeypatch.context() as no_reads:
        no_reads.setattr(ctx.tracker, "get_issue", lambda n: pytest.fail("No extra GitHub read for a flag"))
        assert transition_state(ctx, 1).labels == {"blocked"}
    monkeypatch.setattr(ctx.tracker, "list_comments", lambda n: pytest.fail("No comment-text comparison"))
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out", "Full evidence")
    assert ctx.store.load_flag(1) == flag_record
    assert len(ctx.tracker.comments[1]) == len(first)


@pytest.mark.parametrize("label", ["ready", "running"])
def test_flag_records_removing_each_execution_label(tmp_path, label):
    from foragent.actions import flag
    from foragent.tracker import RecordingTracker

    ctx = _flag_context(tmp_path)
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out")
    RecordingTracker(ctx.tracker, ctx).add_labels(1, [label])
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out")
    assert len(ctx.tracker.list_comments(1)) == 2
    assert label not in ctx.tracker.get_issue(1).labels
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out")
    assert len(ctx.tracker.list_comments(1)) == 2


def test_approval_invalidation_counts_approval_removal_in_the_same_transition(tmp_path):
    from foragent.actions import flag, invalidate_approval
    from foragent.tracker import RecordingTracker

    ctx = _flag_context(tmp_path)
    flag(ctx, 1, Reason.HASH_MISMATCH, "Approval changed")
    RecordingTracker(ctx.tracker, ctx).add_labels(1, ["approved"])
    invalidate_approval(ctx, 1, "Approval changed")
    assert "approved" not in ctx.tracker.get_issue(1).labels
    assert len(ctx.tracker.list_comments(1)) == 2
    invalidate_approval(ctx, 1, "Approval changed")
    assert len(ctx.tracker.list_comments(1)) == 2


def test_transition_uses_only_the_opening_observation_even_if_a_later_read_would_differ(tmp_path):
    from foragent.orch_transition import transition_state

    ctx = _flag_context(tmp_path, ("blocked",))
    opening = ctx.tracker.get_issue(1)
    before = transition_state(ctx, 1, opening)
    ctx.tracker.add_labels(1, ["ready"])
    ctx.store.save_flag(1, "escalated", summary="a later writer")
    ctx.store._atomic_write(ctx.store.root / "hints/1.txt", "a later hint")
    after = before.after(add=["needs-human"], remove=["ready"])
    assert before.labels == {"blocked"} and after.labels == {"blocked", "needs-human"}
    assert after.blocked_reason is None and after.hint is None and after.state == "open"


def test_queued_escalation_counts_ready_removal_without_repeating_unchanged_intent(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch
    from foragent.tracker import RecordingTracker

    case = github_outbox_case(git_history, tmp_path, monkeypatch, action="comment")
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    first = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(first) == 2
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    assert [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"] == first
    RecordingTracker(case.ctx.tracker, case.ctx, queue_writes=True).add_labels(1, ["ready"])
    orch.escalate(case.ctx, 1, "Choose", "Read docs")
    comments = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(comments) == 4


def test_reason_code_and_summary_survive_reopening_and_each_counts_as_a_change(tmp_path):
    from foragent.actions import flag

    ctx = _flag_context(tmp_path)
    flag(ctx, 1, Reason.TIMEOUT, "First reason")
    ctx.store = StateStore(ctx.store.root, now=lambda: NOW)
    assert ctx.store.load_flag(1).summary == "First reason"
    flag(ctx, 1, Reason.TIMEOUT, "First reason")
    assert len(ctx.tracker.list_comments(1)) == 1
    flag(ctx, 1, Reason.STALLED, "First reason")
    flag(ctx, 1, Reason.STALLED, "Second reason")
    assert len(ctx.tracker.list_comments(1)) == 3
    assert ctx.store.load_flag(1).reason == Reason.STALLED
    assert ctx.store.load_flag(1).summary == "Second reason"


def test_unpark_without_a_label_change_journals_but_does_not_comment(git_history, tmp_path, monkeypatch):
    import json

    from foragent import orch_park
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    orch_park.park(case.ctx, 1, "wait")
    before = len(case.tracker.list_comments(1))
    orch_park.unpark(case.ctx, 1)
    assert _new_comments(case, before) == []
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "unparked"
    orch_park.park(case.ctx, 1, "wait", for_person=True)
    orch_park.unpark(case.ctx, 1)
    assert len(_new_comments(case, before)) == 2


def test_queued_repark_observes_restored_blocked_without_reading_old_comments(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch_park
    from foragent.tracker import RecordingTracker

    case = github_outbox_case(git_history, tmp_path, monkeypatch, action="remove_labels")
    orch_park.park(case.ctx, 1, "wait")
    first = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(first) == 1
    orch_park.park(case.ctx, 1, "wait")
    assert [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"] == first
    RecordingTracker(case.ctx.tracker, case.ctx, queue_writes=True).add_labels(1, ["blocked"])
    orch_park.park(case.ctx, 1, "wait")
    comments = [row for row in outbox_rows(persisted_store(case)) if row["action"] == "comment"]
    assert len(comments) == 2, "A queued blocked removal is still a state change"



def test_flag_evidence_is_public_before_a_board_failure(tmp_path, monkeypatch):
    from foragent import board
    from foragent.actions import flag
    from foragent.tracker import TrackerError

    ctx = _flag_context(tmp_path)
    def unavailable(*args, **kwargs):
        raise TrackerError("API rate limit exceeded")
    monkeypatch.setattr(board, "sync_status", unavailable)
    with pytest.raises(TrackerError):
        flag(ctx, 1, Reason.SPEC_QUESTION, "Choose", "English or Korean")
    [comment] = ctx.tracker.list_comments(1)
    assert flag_text(comment.body) == ("Choose", "English or Korean")
    assert len(comment.body.splitlines()) == 1

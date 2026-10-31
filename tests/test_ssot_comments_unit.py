"""Boundaries for one-line transition comments (#1186)."""
from datetime import UTC, datetime
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
    for body in (a, b):  # Both wait in the outbox with the same queue time.
        store.queue_github_write(1, "comment", body=body)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW, config=Config(), repo="owner/repo")
    flag(ctx, 1, Reason.TIMEOUT, "B")
    assert tracker.list_comments(1) == [], "B is already the newest pending blocker"
    flag(ctx, 1, Reason.TIMEOUT, "A")
    assert [c.body for c in tracker.list_comments(1)] == [a], "A after B is a change"


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
    assert len(_new_comments(case, before)) == 2, "A different reason, even by spacing, is recorded"


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
    flag(ctx, 1, Reason.TIMEOUT, "CI timed out", "echo a ↵ echo b")
    bodies = [c.body for c in tracker.list_comments(1)]
    assert len(bodies) == 2, "Different evidence is a new comment"
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
    assert len(_new_comments(case, before)) == 6, "A park reason with a literal separator is a different reason"

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

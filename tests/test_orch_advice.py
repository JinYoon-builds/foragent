"""Accepted advice keeps an issue-owned copy even when GitHub refuses the write (#1185)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from foragent import orch, orch_advice
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import TrackerError
from runner_support import run_case  # noqa: F401 - the shared runner fixture

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


class Refusing:
    """A tracker whose comment write fails with ``error``; it records successful writes."""

    def __init__(self, error=None):
        self.error, self.comments = error, []

    def comment(self, n, body):
        if self.error is not None:
            raise self.error
        self.comments.append((n, body))
        return len(self.comments)


def _context(tmp_path, tracker):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    return SimpleNamespace(store=store, config=Config(repo="owner/repo"), tracker=tracker, now=lambda: NOW)


def _queued(store):
    return [(entry["issue"], entry["body"]) for entry in store.github_outbox() if entry["action"] == "comment"]


@pytest.mark.parametrize("error", [TrackerError("HTTP 502: Bad Gateway"), ValueError("Expecting value"),
                                   OSError("gh not found")])
def test_a_refused_write_waits_in_the_outbox_instead_of_being_lost(tmp_path, error):
    tracker = Refusing(error)
    context = _context(tmp_path, tracker)
    body = orch_advice.talk_body("Preserve the receipt timezone")
    assert orch_advice.publish(tracker, context, 1, body)
    assert _queued(context.store) == [(1, body)]


def test_a_delivered_write_is_not_queued_again(tmp_path):
    tracker = Refusing()
    context = _context(tmp_path, tracker)
    assert orch_advice.publish(tracker, context, 1, "<!-- foragent:talk -->\nhi") is None
    assert tracker.comments == [(1, "<!-- foragent:talk -->\nhi")] and _queued(context.store) == []


def _blocked(c):
    c.store.finish_run(1, outcome="blocked")
    c.tracker.remove_labels(1, ["running"])
    c.tracker.add_labels(1, ["blocked"])


def test_retry_hint_comment_refused_by_github_is_queued_not_lost(run_case, monkeypatch):
    c = run_case
    _blocked(c)

    def refuse(n, body):
        raise TrackerError("HTTP 502: Bad Gateway")

    monkeypatch.setattr(c.tracker, "comment", refuse)
    summary = orch.retry(c.ctx, 1, "Use the selected locale")
    assert "queued for the gate" in summary
    assert [body for _, body in _queued(c.store) if "Use the selected locale" in body]
    assert (c.store.root / "hints/1.txt").read_text() == "Use the selected locale"


def test_retry_hint_reaches_the_issue_byte_for_byte(run_case):
    c = run_case
    _blocked(c)
    hint = "Keep  two spaces\n\n  indented line\nlast\tline"
    orch.retry(c.ctx, 1, hint)
    bodies = [comment.body for comment in c.tracker.list_comments(1)]
    assert any(hint in body for body in bodies), bodies
    assert (c.store.root / "hints/1.txt").read_text() == hint  # The worker receives the same bytes.

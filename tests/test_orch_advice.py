"""Accepted advice keeps an issue-owned copy even when GitHub refuses the write (#1185)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch, orch_advice
from foragent.config import Config
from foragent.reasons import decode_line
from foragent.state import StateStore
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError
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


def _text(body):
    return decode_line(body) if "\n" not in body else body


@pytest.mark.parametrize("error,reported", [(TrackerError("HTTP 502: Bad Gateway"), False),
                                          (ValueError("Expecting value"), True), (OSError("gh not found"), True)])
def test_a_refused_write_waits_in_the_outbox_instead_of_being_lost(tmp_path, error, reported):
    tracker = Refusing(error)
    context = _context(tmp_path, tracker)
    body = orch_advice.talk_body("Preserve the receipt timezone")
    assert bool(orch_advice.publish(tracker, context, 1, body)) == reported
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
        raise ValueError("Invalid comment response")

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
    bodies = [_text(comment.body) for comment in c.tracker.list_comments(1)]
    assert any(hint in body for body in bodies), bodies
    assert (c.store.root / "hints/1.txt").read_text() == hint  # The worker receives the same bytes.


@pytest.mark.parametrize("operation", ["find_pr", "close_pr", "add_labels", "remove_labels"])
def test_retry_publishes_advice_before_any_fallible_transition(run_case, monkeypatch, operation):
    c = run_case
    _blocked(c)
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Old", body="Old")
    hint = "Keep  spacing\n\n  and indentation"

    def refuse(*args, **kwargs):
        # These operations must never see local deliverable advice without its public fact.
        assert any(hint in _text(comment.body) for comment in c.tracker.list_comments(1))
        raise TrackerError("HTTP 502: Bad Gateway")

    monkeypatch.setattr(c.tracker, operation, refuse)
    with pytest.raises(TrackerError, match="502"):
        orch.retry(c.ctx, 1, hint)
    assert (c.store.root / "hints/1.txt").read_text() == hint


@pytest.mark.parametrize("entry", ["retry", "resolve-checks", "resolve-ci"])
@pytest.mark.parametrize("queued", [False, True])
def test_every_retry_path_records_publication_before_hint_write(run_case, monkeypatch, entry, queued):
    from foragent import run_previous

    c = run_case
    _blocked(c)
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Old", body="Old")
    hint = "Keep  spacing\n\n  and indentation"
    append = run_previous.add_hint
    seen = []

    def check_before_save(store, number, message):
        public = [_text(comment.body) for comment in c.tracker.list_comments(number)]
        public += [_text(body) for _, body in _queued(store)]
        assert any(message in body for body in public), public
        seen.append(message)
        append(store, number, message)

    monkeypatch.setattr(orch, "add_hint", check_before_save)
    if queued:
        comment = c.tracker.comment

        def refuse_advice(number, body):
            if number != 1 or body.startswith("<!-- foragent:orch-verdict "):
                return comment(number, body)
            raise TrackerError("HTTP 502: Bad Gateway")

        monkeypatch.setattr(c.tracker, "comment", refuse_advice)
    if entry == "retry":
        orch.retry(c.ctx, 1, hint)
    else:
        outcome = "checks-failed" if entry == "resolve-checks" else "ci-failed"
        monkeypatch.setattr(orch, "review_only", lambda *a, **kw: outcome)
        orch.resolve(c.ctx, 1, "pass", hint)
    assert len(seen) == 1 and seen[0].startswith(hint)


def test_retry_does_not_accept_hint_when_publication_cannot_be_persisted(run_case, monkeypatch):
    c = run_case
    _blocked(c)

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(c.tracker, "comment", refuse)
    monkeypatch.setattr(c.store, "queue_github_write", refuse)
    with pytest.raises(OSError, match="disk full"):
        orch.retry(c.ctx, 1, "Must not become deliverable")
    assert not (c.store.root / "hints/1.txt").exists()


@pytest.mark.parametrize("route", ["github-wait", "pending-write", "rate-limit"])
def test_recording_tracker_queues_each_advice_once(tmp_path, route):
    from datetime import timedelta

    tracker = Refusing(TrackerError("HTTP 429: Too Many Requests"))
    context = _context(tmp_path, tracker)
    before = []
    if route == "github-wait":
        context.store.pause_github(NOW + timedelta(minutes=1))
    elif route == "pending-write":
        context.store.queue_github_write(2, "comment", body="Earlier intent")
        before = [(2, "Earlier intent")]
    body = orch_advice.talk_body("Keep  the receipt\n\n  timezone")
    assert orch_advice.publish(tracker, context, 1, body) is None
    assert _queued(context.store) == [*before, (1, body)]
    assert tracker.comments == []


@pytest.mark.parametrize("error", [TrackerError("HTTP 503"), TrackerError("HTTP 422"), OSError("offline")])
def test_only_accepted_advice_survives_a_later_human_label_change(tmp_path, monkeypatch, error):
    remote = FakeTracker(now=lambda: NOW)
    remote.add_issue(1, "Issue", labels=["blocked"])
    context = _context(tmp_path, remote)
    reader = RecordingTracker(remote, context, queue_writes=True)
    reader.get_issue(1)
    post, failed = remote.comment, [error]

    def unavailable(n, body):
        if failed:
            raise failed.pop()
        return post(n, body)

    monkeypatch.setattr(remote, "comment", unavailable)
    body = orch_advice.talk_body("Keep the authored advice")
    orch_advice.publish(remote, context, 1, body)
    context.store.queue_github_write(1, "comment", body="Stale state announcement")
    remote.add_labels(1, ["needs-human"])
    context.now = lambda: NOW + timedelta(seconds=1)
    context.store._now = context.now
    expected = reader.get_issue(1)
    assert _queued(context.store) == [(1, body)], "Advice remains owed; a superseded state announcement is dropped"
    reader.flush_outbox()
    reader.flush_outbox()
    assert [comment.body for comment in remote.list_comments(1)] == [body]
    assert remote.get_issue(1) == expected and context.store.github_outbox() == []


def test_accepted_advice_publishes_independently_of_pending_label_writes(tmp_path):
    remote = FakeTracker(now=lambda: NOW)
    remote.add_issue(1, "Issue", labels=["blocked"])
    context = _context(tmp_path, remote)
    reader = RecordingTracker(remote, context, queue_writes=True)
    reader.get_issue(1)
    context.store.queue_github_write(1, "add_labels", labels=["ready"])
    body = orch_advice.talk_body("Keep this accepted advice")
    orch_advice.publish(remote, context, 1, body)
    assert [c.body for c in remote.list_comments(1)] == [body]
    assert remote.get_issue(1).labels == {"blocked"}, "Publishing advice must not flush unrelated field writes"
    assert [entry["action"] for entry in context.store.github_outbox()] == ["add_labels"]
    reader.comment(1, "State announcement")
    assert [c.body for c in remote.list_comments(1)] == [body], "Ordinary comments retain the existing FIFO"
    reader.flush_outbox()
    assert [c.body for c in remote.list_comments(1)] == [body, "State announcement"]
    assert remote.get_issue(1).labels == {"blocked", "ready"}

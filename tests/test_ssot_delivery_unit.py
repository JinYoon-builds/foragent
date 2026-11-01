"""A failed announcement remains intent; state comparison never consults its text."""

from dataclasses import asdict
from datetime import timedelta

import pytest

from foragent import orch, orch_park, scheduler
from foragent.reasons import Reason, flag_text, parse_reason
from foragent.state import StateStore
from foragent.tracker import RecordingTracker, TrackerError
from test_ssot_comments_unit import NOW, _flag_context


def _fail_first_comment(monkeypatch, remote, diagnostic):
    post, attempts = remote.comment, []

    def comment(n, body):
        attempts.append(body)
        if len(attempts) == 1:
            raise TrackerError(diagnostic)
        return post(n, body)

    monkeypatch.setattr(remote, "comment", comment)
    return attempts


def _pending_comments(store):
    return [entry for entry in store.github_outbox() if entry["action"] == "comment"]


@pytest.mark.parametrize("diagnostic,queue_writes,needs_human", [
    ("gh: HTTP 503", False, False), ("gh: HTTP/2 500", True, False),
    ("API rate limit exceeded (HTTP 403)", False, True), ("gh: HTTP 429", True, True),
])
def test_failed_flag_comment_survives_reopen_and_unchanged_flag(
    tmp_path, monkeypatch, diagnostic, queue_writes, needs_human,
):
    from foragent.actions import flag
    from foragent.inspect_data import latest_flag

    ctx = _flag_context(tmp_path)
    remote = ctx.tracker
    ctx.tracker = RecordingTracker(remote, ctx, queue_writes=queue_writes)
    attempts = _fail_first_comment(monkeypatch, remote, diagnostic)
    evidence = "English  or Korean\nKeep ↵ this evidence"
    try:
        flag(ctx, 1, Reason.SPEC_QUESTION, "Choose", evidence, needs_human=needs_human)
    except TrackerError:
        pass
    first = _pending_comments(ctx.store)
    assert len(first) == 1 and remote.list_comments(1) == []
    assert flag_text(first[0]["body"]) == ("Choose", evidence)
    assert ctx.tracker.current_issue(1).labels == ({"blocked", "needs-human"} if needs_human else {"blocked"})
    # A fresh process sees the state change already complete, and still owes its original evidence.
    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    flag(ctx, 1, Reason.SPEC_QUESTION, "Choose", evidence, needs_human=needs_human)
    assert _pending_comments(ctx.store) == first
    ctx.tracker.flush_outbox()
    ctx.tracker.flush_outbox()
    comments = remote.list_comments(1)
    assert len(comments) == 1 and len(comments[0].body.splitlines()) == 1
    assert len(attempts) == 2 and attempts[0] == attempts[1] == comments[0].body
    assert parse_reason(comments[0].body) == Reason.SPEC_QUESTION
    assert flag_text(comments[0].body) == ("Choose", evidence)
    assert _pending_comments(ctx.store) == []
    if needs_human:
        assert latest_flag(comments)[1] == "spec-question"
    else:
        [blocked] = orch.list_blocked(ctx)
        assert blocked["reason"] == "spec-question" and blocked["details"] == comments[0].body


@pytest.mark.parametrize("verb,owed", [("retry", 1), ("escalate", 2), ("park", 1), ("stop", 1)])
def test_each_transition_delivers_failed_evidence_once_without_repeating_state_change(
    git_history, tmp_path, monkeypatch, verb, owed,
):
    from foragent.scheduler_stop import _restore_stopped
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    remote, ctx = case.tracker, case.ctx
    if verb == "stop":
        ctx.tracker.add_labels(1, ["running"])
        ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
        ctx.store.finish_run(1, outcome="stopped", detail="Evidence")
    baseline = len(remote.list_comments(1))
    attempts = _fail_first_comment(monkeypatch, remote, "HTTP 503")
    invoke = {
        "retry": lambda: orch.retry(ctx, 1, "Evidence"),
        "escalate": lambda: orch.escalate(ctx, 1, "Choose", "Evidence"),
        "park": lambda: orch_park.park(ctx, 1, "Evidence"),
        "stop": lambda: _restore_stopped(ctx, 1, "Evidence", scheduler.TickReport()),
    }[verb]
    try:
        invoke()
    except TrackerError:
        pass
    first = _pending_comments(ctx.store)
    assert len(first) == owed and all("Evidence" in entry["body"] for entry in first)
    case.store = ctx.store = StateStore(ctx.store.root, now=ctx.now)
    invoke()
    assert _pending_comments(ctx.store) == first
    RecordingTracker(ctx.tracker, ctx, queue_writes=True).flush_outbox()
    invoke()
    RecordingTracker(ctx.tracker, ctx, queue_writes=True).flush_outbox()
    added = remote.list_comments(1)[baseline:]
    assert [comment.body for comment in added] == [entry["body"] for entry in first]
    assert all(len(comment.body.splitlines()) == 1 for comment in added)
    assert len(attempts) == owed + 1


def test_paused_event_retains_changed_comment_even_when_outbox_started_empty(tmp_path):
    ctx = _flag_context(tmp_path)
    ctx.store.pause_github(NOW + timedelta(hours=1))
    assert ctx.store.github_outbox() == []
    orch._event(ctx, 1, "retry", "Evidence", changed=True)
    first = _pending_comments(ctx.store)
    assert len(first) == 1 and "Evidence" in first[0]["body"]
    orch._event(ctx, 1, "retry", "Evidence", changed=False)
    assert _pending_comments(ctx.store) == first and ctx.tracker.list_comments(1) == []
    RecordingTracker(ctx.tracker, ctx, queue_writes=True).flush_outbox()
    assert [c.body for c in ctx.tracker.list_comments(1)] == [first[0]["body"]]


def test_retry_without_hint_clears_pending_hint_once(git_history, tmp_path, monkeypatch):
    from test_orch_verbs import blocked_case

    case, _ = blocked_case(git_history, tmp_path, monkeypatch)
    orch.retry(case.ctx, 1, "Use English")
    path = case.store.root / "hints/1.txt"
    assert path.read_text() == "Use English"
    before = len(case.tracker.list_comments(1))
    orch.retry(case.ctx, 1)
    assert not path.exists(), "Omitting a hint explicitly clears it; it does not preserve the old one"
    added = case.tracker.list_comments(1)[before:]
    assert len(added) == 1 and len(added[0].body.splitlines()) == 1
    orch.retry(case.ctx, 1)
    assert case.tracker.list_comments(1)[before:] == added


@pytest.mark.parametrize("history", [None, "parked", "unparked"])
def test_park_with_identical_observed_state_ignores_history(tmp_path, monkeypatch, history):
    ctx = _flag_context(tmp_path, labels=())
    orch_park.park(ctx, 1, "Wait for the catalog")
    ctx.tracker.comments[1] = []
    if history:
        ctx.tracker.comment(1, f"<!-- foragent:orch kind={history} --> #1: {history}")
    ctx.store.record_comments(1, [asdict(c) for c in ctx.tracker.list_comments(1)])
    before = ctx.tracker.get_issue(1)
    comments = ctx.tracker.list_comments(1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Park's change comparison must not read comment history or outbox")

    monkeypatch.setattr(orch_park, "is_parked", forbidden)
    orch_park.park(ctx, 1, "Wait for the catalog")
    assert ctx.tracker.list_comments(1) == comments
    assert ctx.tracker.get_issue(1).labels == before.labels
    assert ctx.tracker.get_issue(1).body == before.body


def test_legacy_unpark_closes_public_intent_without_label_or_wait_changes(tmp_path):
    ctx = _flag_context(tmp_path, labels=())
    ctx.tracker.comment(1, "<!-- foragent:orch -->\n#1: parked; why: legacy")
    before = ctx.tracker.get_issue(1)
    orch_park.unpark(ctx, 1)
    after = ctx.tracker.get_issue(1)
    assert after.labels == before.labels and after.body == before.body
    assert not orch_park.is_parked(ctx.store, 1)
    comments = ctx.tracker.list_comments(1)
    assert len(comments) == 2 and "kind=unparked" in comments[-1].body
    assert len(comments[-1].body.splitlines()) == 1
    with pytest.raises(orch.OrchError, match="not parked"):
        orch_park.unpark(ctx, 1)
    assert ctx.tracker.list_comments(1) == comments

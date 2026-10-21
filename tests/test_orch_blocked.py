"""Blocker timeout boundaries and inbox behavior without external services."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import board
from foragent.actions import flag
from foragent.config import Config
from foragent.inspect_data import InspectContext, collect
from foragent.reasons import Reason
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from foragent.tracker import Comment, FakeTracker


@pytest.fixture
def ctx(tmp_path):
    now = lambda: datetime(2026, 10, 21, 9, tzinfo=UTC)
    context = TickContext(
        repo="owner/repo", repo_root=tmp_path, tracker=FakeTracker(now=now, strict=True),
        store=StateStore(tmp_path / "state", now=now),
        config=Config(orchestrator_timeout_minutes=5, run_hours="00-01"),
        git=SimpleNamespace(fetch=lambda *args: None), now=now,
        launcher=lambda number: pytest.fail("A blocked issue must not start"),
    )
    context.tracker.add_issue(1, "Blocked work", labels=["blocked"])
    return context


@pytest.mark.parametrize("seconds,escalated", [(299, False), (300, False), (301, True)])
def test_timeout_boundary_outside_run_hours_without_approval(ctx, seconds, escalated):
    started = ctx.now()
    ctx.tracker.comment(1, "<!-- foragent:blocked reason=spec-question -->\nChoose a locale")
    ctx.now = lambda: started + timedelta(seconds=seconds)
    tick(ctx)
    assert ("needs-human" in ctx.tracker.get_issue(1).labels) == escalated
    assert not ctx.tracker.get_issue(1).labels & {"ready", "running"}
    if escalated:
        body = ctx.tracker.list_comments(1)[-1].body
        assert body.startswith("<!-- foragent:needs-human reason=orchestrator-timeout -->\n")
        assert "Choose a locale" in body
        tick(ctx)
        assert len(ctx.tracker.list_comments(1)) == 2


@pytest.mark.parametrize("offline", [False, True])
def test_board_tracks_blocker_until_timeout_without_interrupting_tick(ctx, monkeypatch, caplog, offline):
    from test_board import snapshot

    identity = board.Board(ctx.tracker, ctx.repo, ctx.config).ensure()
    ctx.config = replace(ctx.config, board=identity)
    calls = []
    setter = board.set_status

    def set_status(issue, status):
        calls.append((getattr(issue, "number", issue), status))
        if offline:
            raise OSError("board offline")
        setter(issue, status)

    def status():
        values = snapshot(ctx.tracker, identity)["items"]["nodes"][0]["fieldValues"]["nodes"]
        return next(value["name"] for value in values if value["field"]["name"] == "Status")

    monkeypatch.setattr(board, "set_status", set_status)
    flag(ctx, 1, Reason.SPEC_QUESTION, "Choose a locale")
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert ctx.tracker.list_comments(1)[-1].body.startswith(
        "<!-- foragent:blocked reason=spec-question -->\n")
    if not offline:
        assert status() == "Blocked"

    started = ctx.now()
    ctx.now = lambda: started + timedelta(minutes=5)
    assert tick(ctx).skipped is None
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    if not offline:
        assert status() == "Blocked"

    ctx.now = lambda: started + timedelta(minutes=5, seconds=1)
    assert tick(ctx).skipped is None
    assert ctx.tracker.get_issue(1).labels == {"blocked", "needs-human"}
    assert calls == [(1, "Blocked"), (1, "Blocked"), (1, "Needs human")]
    if offline:
        assert "Board status update failed" in caplog.text
    else:
        assert status() == "Needs human"


@pytest.mark.parametrize("body", [
    "", "Plain discussion", "\n<!-- foragent:blocked reason=stalled -->",
    "<!-- foragent:blocked reason=stalled --> suffix",
    "<!-- foragent:needs-human reason=stalled -->",
])
def test_timeout_requires_a_blocked_comment_on_the_first_line(ctx, body):
    started = ctx.now()
    ctx.tracker.comment(1, body)
    ctx.now = lambda: started + timedelta(hours=1)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert len(ctx.tracker.list_comments(1)) == 1


def test_latest_blocked_comment_uses_timestamp_with_offsets_and_crlf(ctx):
    ctx.tracker.comments[1] = [
        Comment(10, "bot", "<!-- foragent:blocked reason=review-dispute -->\r\nNew",
                "2026-10-21T18:04:00+09:00"),
        Comment(11, "bot", "<!-- foragent:blocked reason=stalled -->\nOld",
                "2026-10-21T09:00:00+00:00"),
    ]
    ctx.now = lambda: datetime(2026, 10, 21, 9, 6, tzinfo=UTC)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}


def test_blocked_pr_is_hidden_until_timeout_escalation(ctx):
    ctx.tracker.comment(1, "<!-- foragent:blocked reason=stalled -->\nReview stalled")
    pr = ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Work", body="Closes #1")
    inbox_ctx = InspectContext(ctx.tracker, ctx.store, ctx.repo, ctx.config, lambda: ctx.now())
    assert collect(inbox_ctx).items == []
    started = ctx.now()
    ctx.now = lambda: started + timedelta(minutes=6)
    tick(ctx)
    item, = collect(inbox_ctx).items
    assert (item.number, item.pr, item.reason) == (1, pr, "orchestrator-timeout")

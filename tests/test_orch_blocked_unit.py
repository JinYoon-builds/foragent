"""Blocker timeout boundaries and inbox behavior without external services."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.inspect_data import InspectContext, collect
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


def test_timeout_clock_starts_at_this_issues_latest_run_end(ctx, tmp_path):
    started = ctx.now()
    ctx.tracker.comment(1, "<!-- foragent:blocked reason=risk-high -->\nOld findings")
    for number, minutes in ((1, 3), (2, 30)):
        ended = StateStore(tmp_path / "state", now=lambda m=minutes: started + timedelta(minutes=m))
        ended.start_run(number, branch=f"foragent/issue-{number}", worktree=ended.worktree_path(number))
        ended.finish_run(number, outcome="blocked")
    ctx.now = lambda: started + timedelta(minutes=8)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    ctx.now = lambda: started + timedelta(minutes=8, seconds=1)
    tick(ctx)
    assert "needs-human" in ctx.tracker.get_issue(1).labels


def test_new_marker_prevents_reapproval_using_an_old_local_hash_mismatch(ctx):
    ctx.store.save_flag(1, "hash-mismatch", flagged_at=(ctx.now() - timedelta(days=1)).isoformat())
    ctx.tracker.comment(1, "<!-- foragent:blocked reason=spec-question -->\nChoose a locale")
    ctx.tracker.record_label(1, "approved", "human")
    assert tick(ctx).skipped is None
    assert ctx.tracker.get_issue(1).labels == {"blocked", "approved"}
    assert ctx.store.load_flag(1).reason == "spec-question"

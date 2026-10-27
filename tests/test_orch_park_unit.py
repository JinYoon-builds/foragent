"""Park as the orchestrator's response to a blocker, without external services."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_park
from foragent.config import Config
from foragent.orch import OrchError
from foragent.orch_events import append_event
from foragent.scheduler import TickContext, TickReport, _escalate_overdue_blocker
from foragent.state import StateStore
from foragent.tracker import FakeTracker

START = datetime(2026, 10, 21, 9, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    now = lambda: START
    context = TickContext(
        repo="owner/repo", repo_root=tmp_path, tracker=FakeTracker(now=now),
        store=StateStore(tmp_path / "state", now=now),
        config=Config(orchestrator_timeout_minutes=5),
        git=SimpleNamespace(fetch=lambda *args: None), now=now,
        launcher=lambda number: pytest.fail("A blocked issue must not start"),
    )
    context.tracker.add_issue(1, "Blocked work", labels=["blocked"])
    context.store.record_labels(1, ["blocked"], remove=False, lanes=context.config.lanes)
    return context



def _escalate(ctx, minutes: int):
    ctx.now = lambda: START + timedelta(minutes=minutes)
    issue = ctx.tracker.get_issue(1)
    return _escalate_overdue_blocker(ctx, issue, TickReport())


@pytest.mark.parametrize("park_minute,escalated", [(1, False), (0, True)])
def test_park_newer_than_the_blocker_answers_it_even_if_the_label_lingers(ctx, park_minute, escalated):
    rows = ctx.store.local_issues()
    rows[0]["comments"] = [{"id": 1, "body": "<!-- foragent:blocked reason=spec-question -->\nx",
                            "created_at": START.isoformat(), "author": "bot"}]
    ctx.store.local_issues = lambda: rows
    if park_minute:
        append_event(ctx.store, START + timedelta(minutes=park_minute), 1, "parked", "#1: parked; why: wait")
    else:
        append_event(ctx.store, START - timedelta(minutes=1), 1, "parked", "#1: parked; why: older")
    issue = _escalate(ctx, 10)
    assert ("needs-human" in issue.labels) == escalated


def test_parked_at_and_is_parked_follow_the_journal(ctx):
    assert orch_park.parked_at(ctx.store, 1) is None and not orch_park.is_parked(ctx.store, 1)
    append_event(ctx.store, START, 1, "parked", "p")
    append_event(ctx.store, START, 2, "unparked", "other issue")
    assert orch_park.parked_at(ctx.store, 1) == START and orch_park.is_parked(ctx.store, 1)
    append_event(ctx.store, START + timedelta(minutes=1), 1, "unparked", "u")
    assert not orch_park.is_parked(ctx.store, 1)
    assert orch_park.parked_at(ctx.store, 1) == START


def test_unpark_without_park_changes_nothing(ctx):
    ctx.tracker.add_labels(1, ["waiting-person"])
    with pytest.raises(OrchError, match="not parked"):
        orch_park.unpark(ctx, 1)
    assert "waiting-person" in ctx.tracker.get_issue(1).labels
    assert not (ctx.store.root / "orch-events.jsonl").exists()


def test_park_refuses_a_closed_issue(ctx):
    ctx.tracker.close_issue(1, "done")
    with pytest.raises(OrchError):
        orch_park.park(ctx, 1, "why")
    assert not (ctx.store.root / "orch-events.jsonl").exists()


def test_park_keeps_approval_labels(ctx):
    ctx.tracker.add_labels(1, ["approved", "needs-human"])
    orch_park.park(ctx, 1, "redesign first")
    assert ctx.tracker.get_issue(1).labels == {"approved"}

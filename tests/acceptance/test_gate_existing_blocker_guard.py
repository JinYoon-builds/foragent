"""Guard (not in Done when): blocker waits that already work on main must keep working.

gate-waits-for-existing-blocker, order blocked-by-existing (Jin r1301, PM r1334). These pass on
main today and must still pass after the change: an open existing blocker whose Done when fails
on main holds a ready issue, its merge (proof on main, closed as completed) starts the issue on
the first pass (once), and plan-key blockers (a sibling issue of the same order) behave as
before. The sibling's Done when fails on main, so both the old rule (close reason) and PM r1334
(Done when on main) give the same answer for it.
"""

import pytest

from foragent import scheduler
from gate_existing_blocker_world import (  # noqa: F401
    BLOCKER, WAITING, advance, blocker_body, close_blocker, deliver, make_world,
    offline_gate_world, started,
)
from test_scheduler import BODY, ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401

pytestmark = pytest.mark.usefixtures("offline_gate_world")

SIBLING = WAITING - 1  # a new issue of the same plan (plan-key blocker)


def test_open_existing_blocker_holds_a_ready_issue_with_free_capacity(ctx, monkeypatch, git_history):
    make_world(ctx, monkeypatch, git_history, BODY)
    assert "ready" in ctx.tracker.get_issue(WAITING).labels and ctx.store.active_runs() == []
    for _ in range(3):
        scheduler.tick(ctx)
        assert not started(ctx), f"#{WAITING} must not start while #{BLOCKER} is open"
        assert ctx.store.active_runs() == []
        advance(ctx, 6)


def test_first_pass_after_merge_starts_the_waiting_issue_once(ctx, monkeypatch, git_history):
    make_world(ctx, monkeypatch, git_history, BODY)
    scheduler.tick(ctx)
    assert not started(ctx)
    advance(ctx, 1)
    deliver(ctx)
    close_blocker(ctx, "completed")
    scheduler.tick(ctx)
    assert started(ctx), f"The first gate pass after #{BLOCKER} merged must start #{WAITING}"
    assert ctx.launched == [WAITING]
    advance(ctx, 1)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING], "The merge starts the waiting issue once"


@pytest.mark.parametrize("reason", ["duplicate", None], ids=["duplicate", "no-reason"])
def test_delivered_blocker_closed_without_reason_or_as_duplicate_starts_once(
        ctx, monkeypatch, git_history, reason):
    """PM r1334 statement 2 for closes main already treats as finished."""
    make_world(ctx, monkeypatch, git_history, BODY)
    scheduler.tick(ctx)
    assert not started(ctx)
    advance(ctx, 1)
    deliver(ctx)
    close_blocker(ctx, reason)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING], f"launched {ctx.launched}"
    advance(ctx, 6)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING], "The waiting issue starts once"


def _plan_key_world(ctx, monkeypatch, git_history):
    make_world(ctx, monkeypatch, git_history, BODY)
    ctx.tracker.remove_blocked_by(WAITING, BLOCKER)
    ctx.tracker.add_issue(SIBLING, "Sibling of the same plan", blocker_body(SIBLING),
                          labels=["approved", "blocked"])
    ctx.tracker.add_blocked_by(WAITING, SIBLING)


def test_plan_key_blocker_open_then_merged_is_unchanged(ctx, monkeypatch, git_history):
    _plan_key_world(ctx, monkeypatch, git_history)
    scheduler.tick(ctx)
    assert not started(ctx)
    advance(ctx, 1)
    deliver(ctx, SIBLING)
    ctx.tracker.close_issue(SIBLING, reason="completed")
    scheduler.tick(ctx)
    assert started(ctx) and ctx.launched == [WAITING]


def test_plan_key_blocker_not_planned_still_holds(ctx, monkeypatch, git_history):
    _plan_key_world(ctx, monkeypatch, git_history)
    scheduler.tick(ctx)
    advance(ctx, 1)
    ctx.tracker.close_issue(SIBLING, reason="not_planned")
    scheduler.tick(ctx)
    assert not started(ctx) and ctx.store.active_runs() == []

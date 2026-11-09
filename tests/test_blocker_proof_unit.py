"""Boundaries of the blocker proof (#2943) beyond the locked acceptance file.

A close reason is never the proof: a blocker closed as completed whose Done when fails on main
holds its waiter. The proof runs in a worker, so a slow Done when does not hold the tick.
"""

from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import scheduler, scheduler_blocker_proof  # noqa: E402
from gate_existing_blocker_world import (  # noqa: E402,F401
    BLOCKER, WAITING, advance, blocker_body, close_blocker, deliver, inbox_rows, make_world,
    mentions_undelivered_close, offline_gate_world, started,
)
from test_scheduler import BODY, ctx  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401
from test_state_split import offline_acceptance  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("offline_gate_world")


def test_completed_close_whose_done_when_fails_on_main_holds_and_tells_once(
        ctx, monkeypatch, capsys, git_history):
    make_world(ctx, monkeypatch, git_history, BODY)
    scheduler.tick(ctx)
    close_blocker(ctx, "completed")
    for _ in range(3):
        advance(ctx, 6)
        scheduler.tick(ctx)
        assert not started(ctx), "A completed close is not proof: the Done when still fails on main"
    rows = inbox_rows(capsys, ctx)
    assert sum(mentions_undelivered_close(row) for row in rows) == 1, rows


def test_a_slow_proof_holds_the_waiter_but_not_the_tick(ctx, monkeypatch, git_history):
    make_world(ctx, monkeypatch, git_history, BODY)
    monkeypatch.setattr(scheduler_blocker_proof, "JOIN_SECONDS", 0.5)
    ctx.tracker.edit_body(BLOCKER, blocker_body(BLOCKER).replace("test -f", "sleep 3 && test -f"))
    deliver(ctx)
    close_blocker(ctx, "not_planned")
    began = time.monotonic()
    scheduler.tick(ctx)
    assert time.monotonic() - began < 2.5, "The tick waits for the proof only up to its join budget"
    assert not started(ctx), "An unknown proof holds the waiting issue"
    result = ctx.store.root / "blocker-proofs" / f"{BLOCKER}.json"
    for _ in range(100):
        if result.exists():
            break
        time.sleep(0.1)
    advance(ctx, 6)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING], "The next pass reads the finished proof and starts once"
    advance(ctx, 6)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING]

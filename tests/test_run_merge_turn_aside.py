"""Merge-turn aside rule (#1760): a holder that must refresh yields to a run that is ready now."""

from dataclasses import replace
from datetime import timedelta

import pytest

from foragent import run_merge_turn as turn
from foragent.runner import run_issue
from runner_support import run_case  # noqa: F401
from test_run_merge_turn import make_ctx, rows


def holders(ctx, seats):
    return [place.n for place in seats if turn.holds_turn(ctx.store, place)]


def test_aside_holder_yields_to_a_ready_run_and_keeps_its_place(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    first, second, third = (turn.seat(ctx, n, "main") for n in (1, 2, 3))
    turn.join(ctx, first, "a")
    turn.join(ctx, second, "b")
    turn.step_aside(ctx, first)
    assert holders(ctx, [first, second]) == [2]
    assert turn.holds_run_turn(ctx.store, 2) and not turn.holds_run_turn(ctx.store, 1)
    assert turn.claim(ctx, turn.Seat(4, "manual", "main"), "d") == 2
    turn.join(ctx, third, "c")  # Joins after the holder stepped aside.
    turn.join(ctx, first, "a2")  # The holder is current and green again.
    assert holders(ctx, [first, second, third]) == [1], "The returning holder is ahead of later runs"
    assert [row["event"] for row in rows(ctx) if row["n"] == 1] == ["ready", "aside", "ready"]


def test_lone_or_all_aside_holders_keep_the_turn_in_order(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a")
    turn.step_aside(ctx, first)
    assert holders(ctx, [first]) == [1] and turn.holds_run_turn(ctx.store, 1)
    turn.join(ctx, second, "b")
    turn.step_aside(ctx, second)
    assert holders(ctx, [first, second]) == [1]
    turn.withdraw(ctx, first)
    assert holders(ctx, [first, second]) == [2]
    turn.join(ctx, first, "a")  # Rejoins at the tail, ready now: it passes the aside seat.
    assert holders(ctx, [first, second]) == [1]
    turn.join(ctx, second, "b")  # Back from aside, the earlier seat leads again.
    assert holders(ctx, [first, second]) == [2]


def test_step_aside_writes_only_for_a_queued_seat_not_already_aside(tmp_path):
    ctx = make_ctx(tmp_path, 1)
    place = turn.seat(ctx, 1, "main")
    turn.step_aside(ctx, place)
    assert not (ctx.store.root / turn.QUEUE).exists()
    turn.join(ctx, place, "a")
    turn.step_aside(ctx, place)
    turn.step_aside(ctx, place)
    turn.join(ctx, place, "a")
    turn.join(ctx, place, "a")
    assert [row["event"] for row in rows(ctx)] == ["ready", "aside", "ready"]
    turn.step_aside(ctx, turn.seat(ctx, 1, "other"))
    assert len(rows(ctx)) == 3, "Another target's queue is separate"


@pytest.mark.parametrize("path", ["implementation", "review-only"])
def test_both_publish_paths_step_aside_while_refreshing_a_moved_base(run_case, monkeypatch, path):
    from foragent import runner

    c = run_case
    if path == "review-only":
        c.ctx.config = replace(c.ctx.config, review="careful")
        assert run_issue(1, c.ctx) == "pr-open"
        c.ctx.config = replace(c.ctx.config, review="fast")
        c.now[0] += timedelta(seconds=1)
    ready = turn.Seat(9, "2026-10-20T08:00:00+00:00", "main")
    live = turn._live
    monkeypatch.setattr(turn, "_live", lambda store, key: key[0] == 9 or live(store, key))
    await_turn, step_aside, seen = turn.await_turn, turn.step_aside, []

    def first_turn(n, ctx, place, head_sha, pr=None):
        result = await_turn(n, ctx, place, head_sha, pr)
        if not seen:
            seen.append("turn")
            turn.join(ctx, ready, "9" * 40)  # Ready now, joined behind the holder.
            c.history.update({"src/moved.py": "VALUE = 1\n"})
        return result

    def aside(ctx, place):
        step_aside(ctx, place)
        seen.append((turn.holds_turn(ctx.store, place), turn.holds_turn(ctx.store, ready)))

    monkeypatch.setattr(turn, "await_turn", first_turn)
    monkeypatch.setattr(turn, "step_aside", aside)
    outcome = (runner.review_only(1, c.ctx, verdict_head=c.tracker.pr_head_sha(2))
               if path == "review-only" else run_issue(1, c.ctx))
    assert outcome == "merged"
    assert seen[:2] == ["turn", (False, True)], "While refreshing, the ready run holds the turn"
    run = c.store.get_run(1)
    own = [row["event"] for row in rows(c.ctx) if row["n"] == 1 and row.get("started_at") == run.started_at]
    assert own[:3] == ["ready", "aside", "ready"] and own[-1] == "turn"

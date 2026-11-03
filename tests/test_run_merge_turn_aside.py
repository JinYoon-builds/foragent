"""Merge-turn aside rule (#1760): a holder that must refresh yields to a run that is ready now."""

from dataclasses import replace
from datetime import timedelta

import pytest

from foragent import run_merge_turn as turn
from foragent.runner import run_issue
from runner_support import run_case  # noqa: F401
from test_run_merge_turn import make_ctx as plain_ctx, rows

MAIN = ["old", "new", "newer", "newest"]  # main's history, oldest first


class Ancestry:
    """A stand-in Git that answers ancestry along MAIN and counts its reads."""

    def __init__(self):
        self.reads = []

    def is_ancestor(self, ancestor, head):
        self.reads.append((ancestor, head))
        if ancestor == "unreadable" or head == "unreadable":
            from foragent.git import GitError
            raise GitError("bad object")
        return ancestor in MAIN and head in MAIN and MAIN.index(ancestor) <= MAIN.index(head)


def make_ctx(tmp_path, *numbers):
    ctx = plain_ctx(tmp_path, *numbers)
    ctx.git = Ancestry()
    return ctx


def holders(ctx, seats):
    return [place.n for place in seats if turn.holds_turn(ctx.store, place)]


def test_aside_holder_yields_to_a_ready_run_and_keeps_its_place(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    first, second, third = (turn.seat(ctx, n, "main") for n in (1, 2, 3))
    turn.join(ctx, first, "a", "old")
    turn.join(ctx, second, "b", "new")
    turn.step_aside(ctx, first, "new")
    assert holders(ctx, [first, second]) == [2]
    assert turn.holds_run_turn(ctx.store, 2) and not turn.holds_run_turn(ctx.store, 1)
    assert turn.claim(ctx, turn.Seat(4, "manual", "main"), "d") == 2
    turn.join(ctx, third, "c", "new")  # Joins after the holder stepped aside.
    turn.join(ctx, first, "a2", "new")  # The holder is current and green again.
    assert holders(ctx, [first, second, third]) == [1], "The returning holder is ahead of later runs"
    assert [row["event"] for row in rows(ctx) if row["n"] == 1] == ["ready", "aside", "back"]


def test_lone_or_all_aside_holders_keep_the_turn_in_order(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a", "old")
    turn.step_aside(ctx, first, "new")
    assert holders(ctx, [first]) == [1] and turn.holds_run_turn(ctx.store, 1)
    turn.join(ctx, second, "b", "old")
    turn.step_aside(ctx, second, "new")
    assert holders(ctx, [first, second]) == [1]
    turn.withdraw(ctx, first)
    assert holders(ctx, [first, second]) == [2]
    turn.join(ctx, first, "a", "new")  # Rejoins at the tail, ready now: it passes the aside seat.
    assert holders(ctx, [first, second]) == [1]
    turn.join(ctx, second, "b", "new")  # Back from aside, the earlier seat leads again.
    assert holders(ctx, [first, second]) == [2]


@pytest.mark.parametrize(("behind", "expected"), [("old", 1), (None, 1), ("new", 2)],
                         ids=["older-base", "unknown-base", "current-base"])
def test_only_a_seat_verified_on_the_aside_base_passes_it(tmp_path, behind, expected):
    """A seat verified on an older base would only refresh the same move again: it keeps waiting."""
    ctx = make_ctx(tmp_path, 1, 2, 3)
    first, second, third = (turn.seat(ctx, n, "main") for n in (1, 2, 3))
    turn.join(ctx, first, "a", "old")
    turn.join(ctx, second, "b", behind)
    turn.step_aside(ctx, first, "new")
    assert holders(ctx, [first, second]) == [expected]
    assert turn.holds_run_turn(ctx.store, expected) and not turn.holds_run_turn(ctx.store, 3 - expected)
    assert turn.claim(ctx, turn.Seat(4, "manual", "main"), "d") == expected
    turn.join(ctx, third, "c", "newer")
    turn.step_aside(ctx, turn.seat(ctx, 2, "main"), "newer")  # The newest aside names the current base.
    if expected == 2:
        assert holders(ctx, [first, second, third]) == [3]


@pytest.mark.parametrize("order", ["merged-then-joined", "joined-then-aside"])
def test_a_seat_on_a_later_main_passes_a_holder_aside_for_an_earlier_one(tmp_path, order):
    """Holder aside onto 'new'; another run merges so main is 'newer'; a run verified on 'newer' passes."""
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a", "old")
    if order == "merged-then-joined":
        turn.step_aside(ctx, first, "new")
        turn.join(ctx, second, "b", "newer")  # The later writer proves new <= newer.
    else:
        turn.join(ctx, second, "b", "newer")
        turn.step_aside(ctx, first, "new")  # The later writer proves it here instead.
    assert holders(ctx, [first, second]) == [2]
    assert turn.holds_run_turn(ctx.store, 2) and turn.claim(ctx, turn.Seat(4, "manual", "main"), "d") == 2
    assert [[["new", "newer"]]] == [row["contains"] for row in rows(ctx) if "contains" in row]


def test_an_aside_seat_that_refreshes_again_names_its_newer_base(tmp_path):
    """Its old aside base must not let a seat verified on that now-stale base pass."""
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a", "old")
    turn.step_aside(ctx, first, "new")
    turn.join(ctx, second, "b", "new")
    assert holders(ctx, [first, second]) == [2]
    turn.step_aside(ctx, first, "newer")  # Main moved again while the holder was aside.
    assert holders(ctx, [first, second]) == [1]
    assert [row.get("base_sha") for row in rows(ctx) if row["n"] == 1 and row["event"] == "aside"] == ["new", "newer"]


@pytest.mark.parametrize("behind", ["unreadable", "old"])
def test_an_unproven_or_older_base_does_not_pass(tmp_path, behind):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a", "old")
    turn.join(ctx, second, "b", behind)
    turn.step_aside(ctx, first, "new")
    assert holders(ctx, [first, second]) == [1]
    assert not any("contains" in row for row in rows(ctx))


def test_step_aside_writes_only_for_a_queued_seat_not_already_aside(tmp_path):
    ctx = make_ctx(tmp_path, 1)
    place = turn.seat(ctx, 1, "main")
    turn.step_aside(ctx, place, "b")
    assert not (ctx.store.root / turn.QUEUE).exists()
    turn.join(ctx, place, "a")
    turn.step_aside(ctx, place, "b")
    turn.step_aside(ctx, place, "b")  # Same base again: nothing new.
    turn.join(ctx, place, "a")
    turn.join(ctx, place, "a")
    assert [row["event"] for row in rows(ctx)] == ["ready", "aside", "back"]
    turn.step_aside(ctx, turn.seat(ctx, 1, "other"), "b")
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

    def first_turn(n, ctx, place, head_sha, pr=None, base_sha=None):
        result = await_turn(n, ctx, place, head_sha, pr, base_sha)
        if not seen:
            seen.append("turn")
            c.history.update({"src/moved.py": "VALUE = 1\n"})
            turn.join(ctx, ready, "9" * 40, c.history.tip)  # Verified on the moved base, behind the holder.
        return result

    def aside(ctx, place, base_sha):
        step_aside(ctx, place, base_sha)
        assert base_sha == c.history.tip
        seen.append((turn.holds_turn(ctx.store, place), turn.holds_turn(ctx.store, ready)))

    monkeypatch.setattr(turn, "await_turn", first_turn)
    monkeypatch.setattr(turn, "step_aside", aside)
    outcome = (runner.review_only(1, c.ctx, verdict_head=c.tracker.pr_head_sha(2))
               if path == "review-only" else run_issue(1, c.ctx))
    assert outcome == "merged"
    assert seen[:2] == ["turn", (False, True)], "While refreshing, the ready run holds the turn"
    run = c.store.get_run(1)
    own = [row["event"] for row in rows(c.ctx) if row["n"] == 1 and row.get("started_at") == run.started_at]
    assert own[:3] == ["ready", "aside", "back"] and own[-1] == "turn"

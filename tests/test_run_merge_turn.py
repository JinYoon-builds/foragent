from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from foragent import run_merge_turn as turn
from foragent.config import Config
from foragent.state import StateStore


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def make_ctx(tmp_path, *numbers):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    for n in numbers:
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n))
    return SimpleNamespace(store=store, now=lambda: clock[0], clock=clock, config=Config())


def rows(ctx):
    return [json.loads(line) for line in (ctx.store.root / turn.QUEUE).read_text().splitlines()]


def test_head_keeps_the_turn_through_its_merge_until_its_run_stops(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, second, "b")
    turn.join(ctx, first, "a")
    turn.join(ctx, second, "b2")  # A changed head keeps the first position.
    assert turn.holds_turn(ctx.store, second) and not turn.holds_turn(ctx.store, first)
    turn.record_turn(ctx, second, "base", "merged")
    # The merge may still be in flight: no handoff on the turn record itself.
    assert turn.holds_turn(ctx.store, second) and not turn.holds_turn(ctx.store, first)
    ctx.store.finish_run(2, outcome="merged")
    assert turn.holds_turn(ctx.store, first)
    assert [row["event"] for row in rows(ctx)] == ["ready", "ready", "turn"]
    assert rows(ctx)[-1] | {"at": None} == {
        "n": 2, "event": "turn", "started_at": second.started_at, "target": "main",
        "members": [2], "base_sha": "base", "result": "merged", "at": None,
    }


def test_withdrawn_entry_rejoins_at_the_tail(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    turn.join(ctx, first, "a")
    turn.join(ctx, second, "b")
    turn.withdraw(ctx, first)
    turn.withdraw(ctx, first)  # Not queued any more: nothing to withdraw.
    assert turn.holds_turn(ctx.store, second)
    turn.join(ctx, first, "a2")
    assert not turn.holds_turn(ctx.store, first)
    assert [row["event"] for row in rows(ctx)] == ["ready", "ready", "withdrawn", "ready"]


def test_dead_entries_are_skipped_and_targets_are_separate(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    ctx.store.finish_run(3, outcome="crashed")
    stale = {"n": 2, "event": "ready", "started_at": "2000-01-01T00:00:00+00:00", "target": "main"}
    legacy = {"n": 3, "event": "ready"}  # No started_at: live only while #3 is running.
    raw = "".join(json.dumps(row) + "\n" for row in (stale, legacy)).encode()
    (ctx.store.root / turn.QUEUE).write_bytes(raw)
    batch = turn.seat(ctx, 2, "foragent/batch-9")
    turn.join(ctx, batch, "c")
    assert turn.holds_turn(ctx.store, batch)
    first = turn.seat(ctx, 1, "main")
    turn.join(ctx, first, "a")
    assert turn.holds_turn(ctx.store, first)
    assert (ctx.store.root / turn.QUEUE).read_bytes().startswith(raw)


def test_waiting_run_sleeps_until_turn_or_time_budget(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, 1, 2)
    flags = []
    monkeypatch.setattr(turn, "_timeout", lambda n, ctx: flags.append(n) or "timeout")
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
    second = turn.seat(ctx, 2, "main")
    ctx.sleep = lambda seconds: ctx.clock.__setitem__(0, ctx.clock[0] + timedelta(seconds=seconds))
    assert turn.await_turn(2, ctx, second, "b") == (True, "timeout")
    assert flags == [2]
    assert ctx.store.get_run(2).phase == "merge-lock-wait"
    ctx.store.finish_run(1, outcome="crashed")
    assert turn.await_turn(2, ctx, second, "b") == (False, None)

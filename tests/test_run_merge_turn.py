from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from foragent import orch_queue, run_merge_turn as turn
from foragent.config import Config
from foragent.runner import run_issue
from foragent.state import StateStore
from runner_support import run_case  # noqa: F401


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def make_ctx(tmp_path, *numbers):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    for n in numbers:
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n))
    return SimpleNamespace(store=store, now=lambda: clock[0], clock=clock, config=Config())


def rows(ctx):
    return [json.loads(line) for line in (ctx.store.root / turn.QUEUE).read_text().splitlines()]


def test_ready_turn_follows_current_global_priority_without_rejoining(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    seats = [turn.seat(ctx, n, "main") for n in (3, 1, 2)]
    for place in seats:
        turn.join(ctx, place, f"head-{place.n}")
    before = (ctx.store.root / turn.QUEUE).read_bytes()

    for priority, expected in [([2, 1], 2), ([1, 2], 1), ([3], 3)]:
        orch_queue.set_global(ctx.store, priority)
        assert [place.n for place in seats if turn.holds_turn(ctx.store, place)] == [expected]
        assert (ctx.store.root / turn.QUEUE).read_bytes() == before


def test_empty_global_priority_preserves_join_order(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    seats = [turn.seat(ctx, n, "main") for n in (3, 1, 2)]
    for place in seats:
        turn.join(ctx, place, f"head-{place.n}")
    orch_queue.set_global(ctx.store, [2])
    orch_queue.set_global(ctx.store, [])

    for expected in seats:
        assert [place.n for place in seats if turn.holds_turn(ctx.store, place)] == [expected.n]
        turn.withdraw(ctx, expected)
    assert not any(turn.holds_turn(ctx.store, place) for place in seats)


@pytest.mark.parametrize(("priority", "expected"), [([], 3), ([2, 1], 2)])
def test_claim_reports_priority_holder_without_joining(tmp_path, priority, expected):
    ctx = make_ctx(tmp_path, 1, 2, 3)
    for n in (3, 1, 2):
        turn.join(ctx, turn.seat(ctx, n, "main"), f"head-{n}")
    orch_queue.set_global(ctx.store, priority)
    before = (ctx.store.root / turn.QUEUE).read_bytes()

    assert turn.claim(ctx, turn.Seat(4, "manual-merge", "main"), "head-4") == expected
    assert (ctx.store.root / turn.QUEUE).read_bytes() == before


@pytest.mark.parametrize(("priority", "expected"), [([], 2), ([1], 1), ([2], 2)])
def test_failed_merge_rejoins_with_fresh_position_within_priority(tmp_path, priority, expected):
    ctx = make_ctx(tmp_path, 1, 2)
    first, second = turn.seat(ctx, 1, "main"), turn.seat(ctx, 2, "main")
    orch_queue.set_global(ctx.store, priority)
    turn.join(ctx, first, "a")
    turn.join(ctx, second, "b")
    ctx.sleep = lambda seconds: ctx.clock.__setitem__(0, ctx.clock[0] + timedelta(seconds=seconds))

    turn.rejoin_after_failed_merge(1, ctx, first, RuntimeError("merge refused"))
    assert not turn.holds_turn(ctx.store, first)
    assert turn.holds_turn(ctx.store, second)
    turn.join(ctx, first, "a")

    assert [place.n for place in (first, second) if turn.holds_turn(ctx.store, place)] == [expected]
    assert [row["event"] for row in rows(ctx)] == ["ready", "ready", "withdrawn", "ready"]


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


@pytest.mark.parametrize("leave", [False, True])
def test_implementation_run_merges_without_withdrawing_and_withdraws_when_it_leaves(run_case, monkeypatch, leave):
    c = run_case
    lock = c.store.merge_lock

    @contextmanager
    def hold_on_turn():
        if leave:  # A policy hold found after joining: the run leaves the ready state unmerged.
            c.tracker.add_labels(1, ["risk:high"])
        with lock():
            yield

    monkeypatch.setattr(c.store, "merge_lock", hold_on_turn)
    outcome = run_issue(1, c.ctx)
    events = [row["event"] for row in rows(c.ctx)]
    if leave:
        assert outcome == "held"
        assert events == ["ready", "withdrawn"]
    else:
        assert outcome == "merged"
        assert events == ["ready", "turn"], "A merged run keeps its entry until its run stops"


def test_waiting_run_rechecks_ci_on_its_turn_and_withdraws_when_red(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    ahead_live = queue_behind_live_run(c, monkeypatch)
    c.tracker.set_pr_failed_logs(2, "Broke while waiting")
    sleeps = []

    def sleep(seconds):
        # While run 1 waits, CI on its head turns red and the run ahead finishes.
        sleeps.append(seconds)
        c.tracker.set_pr_checks(2, "fail")
        ahead_live[0] = False

    c.ctx.sleep = sleep
    assert run_issue(1, c.ctx) == "ci-failed"
    assert len(sleeps) == 1, "A waiting run does not poll CI; it checks once it holds the turn"
    assert c.tracker.merged_prs == []
    own = [row["event"] for row in rows(c.ctx) if row["n"] == 1]
    assert own == ["ready", "withdrawn"]


def test_ci_wait_deadline_starts_when_the_turn_arrives(run_case, monkeypatch):
    c = run_case
    ahead_live = queue_behind_live_run(c, monkeypatch)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if ahead_live[0]:
            # A long turn wait (past the 1,200 s CI deadline); a rerun is pending afterwards.
            c.now[0] += timedelta(minutes=25)
            c.tracker.set_pr_checks(2, "pending")
            ahead_live[0] = False
        else:
            c.now[0] += timedelta(seconds=seconds)
            c.tracker.set_pr_checks(2, "pass")

    c.ctx.sleep = sleep
    assert run_issue(1, c.ctx) == "merged"
    assert len(sleeps) == 2
    assert c.tracker.merged_prs == [2]


def queue_behind_live_run(c, monkeypatch):
    """Put a live run (#9) ahead in the queue; only its liveness is simulated."""
    ahead_live = [True]
    live = turn._live
    monkeypatch.setattr(turn, "_live", lambda store, key: ahead_live[0] if key[0] == 9 else live(store, key))
    turn.join(c.ctx, turn.Seat(9, "2026-10-20T08:00:00+00:00", "main"), "a" * 40)
    return ahead_live


@pytest.mark.parametrize("path", ["implementation", "review-only"])
def test_failed_merge_rejoins_at_the_tail_and_takes_a_later_turn(run_case, monkeypatch, path):
    from foragent import runner
    from foragent.tracker import TrackerError

    c = run_case
    if path == "review-only":
        c.ctx.config = replace(c.ctx.config, review="careful")
        assert run_issue(1, c.ctx) == "pr-open"
        c.ctx.config = replace(c.ctx.config, review="fast")
        c.now[0] += timedelta(seconds=1)
    started = c.store.get_run(1).started_at
    merge, tries, sleeps = c.tracker.merge_pr, [], []

    def flaky(pr, **kwargs):
        tries.append(pr)
        if len(tries) == 1:
            raise TrackerError("Pull request is not mergeable right now")
        return merge(pr, **kwargs)

    monkeypatch.setattr(c.tracker, "merge_pr", flaky)
    c.ctx.sleep = lambda seconds: sleeps.append(seconds) or c.now.__setitem__(0, c.now[0] + timedelta(seconds=seconds))
    outcome = (runner.review_only(1, c.ctx, verdict_head=c.tracker.pr_head_sha(2))
               if path == "review-only" else run_issue(1, c.ctx))
    assert outcome == "merged"
    assert len(tries) == 2 and c.tracker.merged_prs == [2]
    own = [(row["event"], row.get("result")) for row in rows(c.ctx)
           if row["n"] == 1 and row.get("started_at") == c.store.get_run(1).started_at]
    assert own == [("ready", None), ("turn", "merged"), ("turn", "failed"), ("withdrawn", None),
                   ("ready", None), ("turn", "merged")]
    assert sleeps == [30], "The retry is spaced out once before rejoining"
    assert "rejoining the merge queue: Pull request is not mergeable" in c.store.log_path(1).read_text()
    assert path == "review-only" or started == c.store.get_run(1).started_at

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
    return SimpleNamespace(store=store, now=lambda: clock[0], clock=clock, config=Config(),
                           monotonic=lambda: (clock[0] - NOW).total_seconds())


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
    from foragent.runner import _remaining

    ctx = make_ctx(tmp_path, 1, 2)
    ctx.config = replace(ctx.config, max_runtime_minutes=1)
    ctx.clock[0] += timedelta(seconds=40)
    ctx.store.update_run(2, phase="checking")
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
    second = turn.seat(ctx, 2, "main")
    sleeps = []

    def sleep(seconds):
        run = ctx.store.get_run(2)
        assert run.phase == "merge-lock-wait"
        assert datetime.fromisoformat(run.waiting_until) > ctx.now()
        assert _remaining(2, ctx) == 20
        assert run.waited_seconds == sum(sleeps)
        sleeps.append(seconds)
        ctx.clock[0] += timedelta(seconds=seconds)
        if sum(sleeps) >= 90:
            ctx.store.finish_run(1, outcome="merged")

    ctx.sleep = sleep
    assert turn.await_turn(2, ctx, second, "b") == (True, None)
    assert sleeps == [30, 30, 30]
    run = ctx.store.get_run(2)
    assert run.waited_seconds == 90
    assert run.waiting_until is None and run.pause_regions == {}
    assert run.phase == "checking"
    assert ctx.store.load_flag(2) is None
    assert _remaining(2, ctx) == 20
    assert turn.await_turn(2, ctx, second, "b") == (False, None)
    assert ctx.store.get_run(2).waited_seconds == 90
    ctx.clock[0] += timedelta(seconds=21)
    assert _remaining(2, ctx) == 0


def test_interrupted_merge_wait_credits_elapsed_time_and_restores_phase(tmp_path):
    ctx = make_ctx(tmp_path, 1, 2)
    ctx.store.update_run(2, phase="checking")
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")

    def sleep(seconds):
        ctx.clock[0] += timedelta(seconds=7)
        raise RuntimeError("interrupted")

    ctx.sleep = sleep
    with pytest.raises(RuntimeError, match="interrupted"):
        turn.await_turn(2, ctx, turn.seat(ctx, 2, "main"), "b")
    run = ctx.store.get_run(2)
    assert run.waited_seconds == 7
    assert run.waiting_until is None and run.pause_regions == {}
    assert run.phase == "checking"


@pytest.mark.parametrize("path", ["queue", "retry"])
def test_wait_limit_releases_live_seat_before_reporting(tmp_path, monkeypatch, path):
    from foragent import orch_inbox
    from foragent.run_attempt import ExecutorStopped

    ctx = make_ctx(tmp_path, 1, 2, 3)
    first, own, following = (turn.seat(ctx, n, "main") for n in (1, 2, 3))
    ctx.tracker = SimpleNamespace(add_labels=lambda *_: None, remove_labels=lambda *_: None)
    monkeypatch.setattr(turn, "monotonic", lambda: 0.0)
    monkeypatch.setattr(turn, "MERGE_WAIT_SECONDS", 30)
    if path == "queue":
        turn.join(ctx, first, "a")
    else:
        turn.join(ctx, own, "b")
        turn.join(ctx, following, "c")

    def sleep(seconds):
        assert seconds == 30
        if path == "queue":
            turn.join(ctx, following, "c")
        else:
            # A refused merge must let the next run proceed during its pause.
            assert turn.holds_turn(ctx.store, following)
        ctx.clock[0] += timedelta(seconds=seconds)

    report = orch_inbox.add_request
    reports = []

    def report_after_release(*args, **kwargs):
        assert ctx.store.get_run(2).status == "running"
        assert own.key not in turn._waiting(rows(ctx), "main")
        ctx.store.finish_run(1, outcome="merged")
        assert turn.holds_turn(ctx.store, following)
        reports.append(report(*args, **kwargs))

    ctx.sleep = sleep
    monkeypatch.setattr(orch_inbox, "add_request", report_after_release)
    with pytest.raises(ExecutorStopped, match="Merge-turn wait limit"):
        if path == "queue":
            turn.await_turn(2, ctx, own, "b")
        else:
            turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"))
    assert len(reports) == 1
    turn.withdraw(ctx, own)  # The caller's final cleanup must remain idempotent.
    assert [row["event"] for row in rows(ctx) if row["n"] == 2] == ["ready", "withdrawn"]
    run = ctx.store.get_run(2)
    assert run.status == "running" and run.waited_seconds == 30
    assert run.waiting_until is None and run.pause_regions == {}


@pytest.mark.parametrize("path", ["queue", "retry"])
def test_fast_polls_do_not_spend_merge_wait_limit(tmp_path, monkeypatch, path):
    ctx = make_ctx(tmp_path, 1, 2)
    own = turn.seat(ctx, 2, "main")
    measured = [0.0]
    sleeps = []
    monkeypatch.setattr(turn, "monotonic", lambda: measured[0])
    monkeypatch.setattr(turn, "MERGE_WAIT_SECONDS", 60)
    ctx.tracker = SimpleNamespace(add_labels=lambda *_: None, remove_labels=lambda *_: None)

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= 150
        measured[0] += 0.01
        if len(sleeps) == 150:
            ctx.store.finish_run(1, outcome="merged")

    ctx.sleep = sleep
    if path == "queue":
        turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
        assert turn.await_turn(2, ctx, own, "b") == (True, None)
    else:
        for _ in range(150):
            turn.join(ctx, own, "b")
            turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"))
    run = ctx.store.get_run(2)
    assert len(sleeps) == run.merge_wait_polls == 150
    assert run.merge_waited_seconds == pytest.approx(1.5)
    assert run.status == "running" and run.waited_seconds == 0
    assert run.waiting_until is None and run.pause_regions == {}


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
@pytest.mark.parametrize("advance_clock", [True, False], ids=["elapsed-wait", "frozen-clock"])
def test_merge_wait_limit_preserves_pr_and_reports_elapsed_wait(run_case, monkeypatch, path, advance_clock):
    from foragent import orch_inbox, runner

    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    if path == "review-only":
        c.ctx.config = replace(c.ctx.config, review="careful")
        assert run_issue(1, c.ctx) == "pr-open"
        c.now[0] += timedelta(seconds=1)
        c.ctx.config = replace(c.ctx.config, review="fast")
    queue_behind_live_run(c, monkeypatch)
    monkeypatch.setattr(turn, "MERGE_WAIT_SECONDS", 90)
    measured = [0.0]
    monkeypatch.setattr(turn, "monotonic", lambda: measured[0])
    sleeps, remaining = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= 3, "The wait must end even when the run clock is frozen"
        remaining.append(runner._remaining(1, c.ctx))
        measured[0] += seconds
        if advance_clock:
            c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    outcome = (runner.review_only(1, c.ctx, verdict_head=c.tracker.pr_head_sha(2))
               if path == "review-only" else run_issue(1, c.ctx))
    assert outcome == "merge-wait"
    assert sleeps == [30, 30, 30] and remaining == [60, 60, 60]
    run = StateStore(c.store.root).get_run(1)
    assert run.outcome == "merge-wait" and run.merge_wait_polls == 3
    elapsed = 90
    assert run.merge_waited_seconds == elapsed
    assert run.waited_seconds == (elapsed if advance_clock else 0)
    assert run.waiting_until is None and run.pause_regions == {}
    assert f"after {elapsed}s (3 polls)" in run.detail
    requests = orch_inbox.requests(c.store, pending=True)
    assert len(requests) == 1
    request = requests[0]
    assert request["kind"] == "merge-wait" and request["issue"] == 1
    assert request["waited_seconds"] == elapsed and request["polls"] == 3
    assert c.tracker.find_pr("foragent/issue-1") == 2
    assert c.tracker.get_issue(2).state == "open" and c.tracker.merged_prs == []
    assert len(c.tracker.created_prs) == 1
    assert c.store.load_flag(1) is None
    assert c.tracker.get_issue(1).labels & {"blocked"}
    assert not c.tracker.get_issue(1).labels & {"running", "ready", "needs-human"}
    comments = "\n".join(row.body for row in c.tracker.list_comments(1))
    assert "Too big for one session" not in comments and "split it in order" not in comments
    assert [row["event"] for row in rows(c.ctx) if row["n"] == 1
            and row.get("started_at") == run.started_at][-1] == "withdrawn"


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
    assert c.store.get_run(1).waited_seconds == 30
    assert "rejoining the merge queue: Pull request is not mergeable" in c.store.log_path(1).read_text()
    assert path == "review-only" or started == c.store.get_run(1).started_at


class Reads:
    """A tracker that answers issue state by number and records every read."""

    def __init__(self, states=None, errors=None):
        self.states, self.errors = states or {}, errors or {}
        self.reads, self.removed = [], []

    def get_issue(self, number):
        self.reads.append(number)
        if number in self.errors:
            raise self.errors[number]
        return SimpleNamespace(state=self.states.get(number, "open"))

    def add_labels(self, n, labels):
        pass

    def remove_labels(self, n, labels):
        self.removed.append((n, set(labels)))


def waited_ctx(tmp_path, monkeypatch, tracker, waited=0):
    ctx = make_ctx(tmp_path, 1, 2)
    ctx.tracker = tracker
    ctx.store.update_run(2, merge_waited_seconds=waited)
    monkeypatch.setattr(turn, "monotonic", ctx.monotonic)
    ctx.sleep = lambda seconds: ctx.clock.__setitem__(0, ctx.clock[0] + timedelta(seconds=seconds))
    return ctx


def test_merge_wait_ceiling_is_sixteen_hours_with_five_minute_checks():
    assert turn.MERGE_WAIT_SECONDS == 57_600
    assert turn.REMOTE_CHECK_SECONDS == 300


@pytest.mark.parametrize("closed", [2, 7], ids=["issue", "pr"])
def test_retry_pause_ends_on_a_remote_fact_without_reporting(tmp_path, monkeypatch, closed):
    from foragent import orch_events, orch_inbox
    from foragent.run_attempt import ExecutorStopped

    tracker = Reads({closed: "closed"})
    ctx = waited_ctx(tmp_path, monkeypatch, tracker, waited=299)
    own = turn.seat(ctx, 2, "main")
    turn.join(ctx, own, "b")
    with pytest.raises(ExecutorStopped) as stopped:
        turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), pr=7)
    assert stopped.value.outcome == "remote-ended"
    assert tracker.reads == ([2] if closed == 2 else [2, 7]), "An ended issue needs no PR read"
    assert ctx.store.get_run(2).merge_wait_polls == 1, "The first read was not yet due"
    assert [row["event"] for row in rows(ctx)] == ["ready", "withdrawn"]
    assert (2, {"ready", "running"}) in tracker.removed
    assert orch_inbox.requests(ctx.store, pending=True) == []
    assert orch_events.events(ctx.store, "merge-wait-limit") == []
    assert ctx.store.load_flag(2) is None


@pytest.mark.parametrize("error", ["HTTP 502: bad gateway", "HTTP 404: Not Found"], ids=["transport", "missing"])
def test_read_failure_is_unknown_and_the_next_check_is_a_full_interval_later(tmp_path, monkeypatch, error):
    from foragent.tracker import TrackerError

    tracker = Reads(errors={2: TrackerError(error)})
    ctx = waited_ctx(tmp_path, monkeypatch, tracker, waited=299)
    own = turn.seat(ctx, 2, "main")
    for _ in range(10):  # The first pause wakes at the 300 s read; nine more of 30 s stay below 600 s.
        turn.join(ctx, own, "b")
        turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), pr=7)
    assert tracker.reads == [2, 7], "A failed read and re-entry must not bring a check forward"
    assert "could not read Issue #2" in ctx.store.log_path(2).read_text()
    tracker.errors.clear()
    tracker.states[7] = "closed"
    turn.join(ctx, own, "b")
    from foragent.run_attempt import ExecutorStopped

    with pytest.raises(ExecutorStopped, match="PR #7 ended"):
        turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), pr=7)
    assert tracker.reads == [2, 7, 2, 7]


def test_unknown_pr_reads_only_the_issue_and_never_guesses_an_end(tmp_path, monkeypatch):
    tracker = Reads()
    ctx = waited_ctx(tmp_path, monkeypatch, tracker, waited=299)
    own = turn.seat(ctx, 2, "main")
    turn.join(ctx, own, "b")
    turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"))
    assert tracker.reads == [2]
    assert ctx.store.get_run(2).status == "running"


def test_due_check_at_the_ceiling_prefers_a_remote_end_over_a_limit_hit(tmp_path, monkeypatch):
    from foragent import orch_events, orch_inbox
    from foragent.run_attempt import ExecutorStopped

    ctx = waited_ctx(tmp_path, monkeypatch, Reads({2: "closed"}), waited=turn.MERGE_WAIT_SECONDS - 30)
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
    with pytest.raises(ExecutorStopped) as stopped:
        turn.await_turn(2, ctx, turn.seat(ctx, 2, "main"), "b", 7)
    assert stopped.value.outcome == "remote-ended"
    assert [row["event"] for row in rows(ctx) if row["n"] == 2] == ["ready", "withdrawn"]
    assert orch_inbox.requests(ctx.store, pending=True) == []
    assert orch_events.events(ctx.store, "merge-wait-limit") == []


@pytest.mark.parametrize("path", ["queue", "retry"])
def test_each_limit_hit_is_one_event_per_execution_and_survives_reload(tmp_path, monkeypatch, path):
    from foragent import orch_events, orch_inbox
    from foragent.run_attempt import ExecutorStopped

    ctx = waited_ctx(tmp_path, monkeypatch, Reads())
    monkeypatch.setattr(turn, "MERGE_WAIT_SECONDS", 30)
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
    started = []
    for _ in range(2):
        ctx.clock[0] += timedelta(seconds=1)
        run = ctx.store.start_run(2, branch="foragent/issue-2", worktree=ctx.store.worktree_path(2))
        started.append(run.started_at)
        own = turn.seat(ctx, 2, "main")
        with pytest.raises(ExecutorStopped, match="Merge-turn wait limit"):
            if path == "queue":
                turn.await_turn(2, ctx, own, "b", 7)
            else:
                turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), 7)
        turn.withdraw(ctx, own)
        ctx.store.finish_run(2, outcome="merge-wait")
    hits = orch_events.events(StateStore(ctx.store.root), "merge-wait-limit")
    assert [hit["run_started_at"] for hit in hits] == started and len(set(started)) == 2
    assert all(hit["issue"] == 2 and hit["waited_seconds"] == 30 and hit["limit_seconds"] == 30 for hit in hits)
    assert len(orch_inbox.requests(ctx.store, pending=True)) >= 1


def test_review_only_run_stops_quietly_when_its_pr_closes_with_the_issue_open(run_case, monkeypatch):
    from foragent import orch_inbox, runner

    c = run_case
    c.ctx.config = replace(c.ctx.config, review="careful")
    assert run_issue(1, c.ctx) == "pr-open"
    c.now[0] += timedelta(seconds=1)
    c.ctx.config = replace(c.ctx.config, review="fast")
    queue_behind_live_run(c, monkeypatch)
    monkeypatch.setattr(turn, "monotonic", c.ctx.monotonic)

    def sleep(seconds):
        c.now[0] += timedelta(seconds=seconds)
        if c.store.get_run(1).merge_waited_seconds >= 330:
            c.tracker.issues[2] = replace(c.tracker.issues[2], state="closed")

    c.ctx.sleep = sleep
    head = c.tracker.pr_head_sha(2)
    assert runner.review_only(1, c.ctx, verdict_head=head) == "remote-ended"
    assert c.store.load_flag(1) is None
    labels = c.tracker.get_issue(1).labels
    assert not labels & {"running", "ready", "blocked", "needs-human"}
    assert c.tracker.get_issue(1).state == "open" and c.tracker.get_issue(2).state == "closed"
    assert c.tracker.merged_prs == []
    assert orch_inbox.requests(c.store, pending=True) == []
    assert c.store.get_run(1).outcome == "remote-ended"


@pytest.mark.parametrize("path", ["queue", "retry"])
@pytest.mark.parametrize(("first", "later"), [(5, 5), (10, 1), (1, 10)], ids=["even", "slow-first", "slow-later"])
def test_slow_reads_keep_every_check_of_each_fact_a_full_interval_apart(tmp_path, monkeypatch, path, first, later):
    ctx = waited_ctx(tmp_path, monkeypatch, None)
    own = turn.seat(ctx, 2, "main")
    read_at = {2: [], 7: []}

    class SlowReads(Reads):
        def get_issue(self, number):
            read_at[number].append((ctx.clock[0] - NOW).total_seconds())
            ctx.clock[0] += timedelta(seconds=first if len(read_at[number]) == 1 else later)
            return super().get_issue(number)

    ctx.tracker = SlowReads()
    sleep = ctx.sleep

    def bounded(seconds):
        assert seconds > 0
        sleep(seconds)
        if len(read_at[7]) == 5:
            ctx.store.finish_run(1, outcome="merged")

    ctx.sleep = bounded
    if path == "queue":
        turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
        assert turn.await_turn(2, ctx, own, "b", 7) == (True, None)
    else:
        while len(read_at[7]) < 5:
            turn.join(ctx, own, "b")
            turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), 7)
    for number, times in read_at.items():
        assert len(times) >= 5, times
        gaps = [b - a for a, b in zip(times, times[1:])]
        # Reads run in turn, so a PR read may start late behind a slow issue read, but never early.
        assert all(300 <= gap <= 300 + max(first, later) for gap in gaps), (number, times)
    assert {b - a for a, b in zip(read_at[2], read_at[2][1:])} == {300}, read_at[2]
    assert read_at[2][0] == 300 and read_at[7][0] == 300 + first


def test_turn_released_at_a_due_read_still_reads_first(tmp_path, monkeypatch):
    from foragent.run_attempt import ExecutorStopped

    tracker = Reads()
    ctx = waited_ctx(tmp_path, monkeypatch, tracker)
    own = turn.seat(ctx, 2, "main")
    turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
    sleep = ctx.sleep

    def sleep_and_release(seconds):
        sleep(seconds)
        waited = (ctx.clock[0] - NOW).total_seconds()
        if waited >= 330:
            tracker.states[2] = "closed"
        if waited >= 600:
            ctx.store.finish_run(1, outcome="merged")

    ctx.sleep = sleep_and_release
    with pytest.raises(ExecutorStopped) as stopped:
        turn.await_turn(2, ctx, own, "b", 7)
    assert stopped.value.outcome == "remote-ended"
    assert turn.holds_turn(ctx.store, turn.seat(ctx, 1, "main")) is False


def test_holding_the_turn_on_entry_still_makes_a_read_that_is_already_due(tmp_path, monkeypatch):
    from foragent.run_attempt import ExecutorStopped

    tracker = Reads()
    ctx = waited_ctx(tmp_path, monkeypatch, tracker, waited=299)
    own = turn.seat(ctx, 2, "main")
    turn.join(ctx, own, "b")
    turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), 7)
    assert tracker.reads == [2, 7]
    # Closing the last wait can credit past the next due point with no read after it.
    ctx.store.update_run(2, merge_waited_seconds=600.5)
    assert turn.await_turn(2, ctx, own, "b", 7) == (True, None), "A read is made, then the turn is taken"
    assert tracker.reads == [2, 7, 2, 7]
    assert turn.await_turn(2, ctx, own, "b", 7) == (False, None), "Nothing is due now: no wait and no read"
    assert tracker.reads == [2, 7, 2, 7]
    ctx.store.update_run(2, merge_waited_seconds=901)
    tracker.states[7] = "closed"
    with pytest.raises(ExecutorStopped, match="PR #7 ended"):
        turn.await_turn(2, ctx, own, "b", 7)


@pytest.mark.parametrize("path", ["implementation", "review-only"])
def test_issue_closed_before_a_turn_released_at_the_due_read_never_merges(run_case, monkeypatch, path):
    from foragent import runner

    c = run_case
    if path == "review-only":
        c.ctx.config = replace(c.ctx.config, review="careful")
        assert run_issue(1, c.ctx) == "pr-open"
        c.now[0] += timedelta(seconds=1)
        c.ctx.config = replace(c.ctx.config, review="fast")
    ahead_live = queue_behind_live_run(c, monkeypatch)
    monkeypatch.setattr(turn, "monotonic", c.ctx.monotonic)
    waited = [0]

    def sleep(seconds):
        c.now[0] += timedelta(seconds=seconds)
        waited[0] += seconds
        if waited[0] >= 330:
            c.tracker.close_issue(1)
        if waited[0] >= 600:
            ahead_live[0] = False

    c.ctx.sleep = sleep
    outcome = (runner.review_only(1, c.ctx, verdict_head=c.tracker.pr_head_sha(2))
               if path == "review-only" else run_issue(1, c.ctx))
    assert outcome == "remote-ended"
    assert c.tracker.merged_prs == []
    assert waited[0] == 600


@pytest.mark.parametrize("path", ["queue", "retry"])
@pytest.mark.parametrize("stop", ["remote-ended", "merge-wait"])
def test_a_stopping_run_never_queues_for_a_machine_slot(tmp_path, monkeypatch, path, stop):
    from foragent import capacity
    from foragent.run_attempt import ExecutorStopped

    states = {2: "closed"} if stop == "remote-ended" else {}
    ctx = waited_ctx(tmp_path, monkeypatch, Reads(states), waited=turn.MERGE_WAIT_SECONDS - 30)
    if stop == "remote-ended":
        monkeypatch.setattr(turn, "MERGE_WAIT_SECONDS", 10 * turn.MERGE_WAIT_SECONDS)
    ctx.store.update_run(2, phase="merging")
    own = turn.seat(ctx, 2, "main")
    deadlines = []

    def full_machine(store, n, identity, *, deadline, **_):
        deadlines.append(deadline)
        assert deadline is not None, "Ending behind a full machine would keep the run and its claims forever"
        raise TimeoutError("machine is full")

    monkeypatch.setattr(capacity, "admit_run", full_machine)
    with pytest.raises(ExecutorStopped) as stopped:
        if path == "queue":
            turn.join(ctx, turn.seat(ctx, 1, "main"), "a")
            turn.await_turn(2, ctx, own, "b", 7)
        else:
            turn.join(ctx, own, "b")
            turn.rejoin_after_failed_merge(2, ctx, own, RuntimeError("merge refused"), 7)
    assert stopped.value.outcome == stop
    assert deadlines, "The stop must have tried to restore the interrupted phase"
    run = ctx.store.get_run(2)
    assert run.status == "running" and run.waiting_until is None and run.pause_regions == {}
    assert [row["event"] for row in rows(ctx) if row["n"] == 2][-1] == "withdrawn"

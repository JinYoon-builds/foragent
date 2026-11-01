from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from foragent.reasons import Reason
from foragent.runner import _executor_wait, _merge_lock, run_issue
from foragent.state import StateStore
from foragent.tracker import TrackerError

from conftest import BASE_TIME
from runner_support import conflicting_lanes, run_case, scripted_results


@pytest.fixture
def slot_case(tmp_path, monkeypatch):
    import os

    from foragent import capacity
    from foragent.config import Config

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "own", now=lambda: clock[0])
    other = StateStore(tmp_path / "other", now=lambda: clock[0])
    store.start_run(1, branch="b", worktree="w", pid=os.getpid())
    store.update_run(1, phase="reviewing", phase_started_at=clock[0].isoformat())
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: clock[0],
                          monotonic=lambda: (clock[0] - BASE_TIME).total_seconds())
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        pytest.fail("An abandoned operation must not queue for a machine slot")

    def occupy():
        other.start_run(9, branch="other", worktree="other", pid=os.getpid())

    capacity.write_state(store, 1, clock[0])
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "time", SimpleNamespace(monotonic=ctx.monotonic, sleep=sleep))
    return SimpleNamespace(ctx=ctx, store=store, other=other, clock=clock, sleeps=sleeps, occupy=occupy)


@pytest.mark.parametrize("kind", ["credited", "lazy-credited", "merge-lock", "merge-turn", "executor", "test-slot",
                                   "context", "baseline", "local-baseline"])
@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failed_phase_restoration_does_not_reenter_or_replace_the_exception(slot_case, monkeypatch, kind, exception):
    from foragent import capacity, run_waits

    c, error = slot_case, exception("original failure")

    def fail(*args, **kwargs):
        run_waits._phase(1, c.ctx, "overlap-wait")
        c.occupy()
        c.clock[0] += timedelta(seconds=3)
        raise error

    with pytest.raises(exception) as caught:
        _interrupt_wait(c, monkeypatch, kind, fail)
    assert caught.value is error and c.sleeps == []
    assert c.store.get_run(1).phase in capacity.IDLE_RUN_PHASES
    assert c.store.get_run(1).waiting_until is None
    assert capacity.machine_runs(c.store, c.ctx.now()) == 1
    if kind in {"credited", "lazy-credited", "merge-lock", "merge-turn", "executor", "test-slot"}:
        assert c.store.get_run(1).waited_seconds == 3


INTERRUPTED_WAITS = ["credited", "lazy-credited", "merge-lock", "merge-turn", "executor", "test-slot",
                     "context", "baseline", "local-baseline", "group-ci"]


def _interrupt_wait(c, monkeypatch, kind, fail):
    from foragent import capacity, ci_local, run_baseline, run_merge_turn, run_waits

    @contextmanager
    def failed_lock():
        fail()
        yield

    @contextmanager
    def failed_test_slot(slots, *, waiting):
        waiting()
        fail()
        yield

    if kind in {"credited", "lazy-credited"}:
        run_waits._credited_wait(1, c.ctx, fail, "overlap-wait", eager=kind == "credited")
    elif kind == "merge-lock":
        monkeypatch.setattr(c.store, "merge_lock", failed_lock)
        with _merge_lock(1, c.ctx):
            pytest.fail("A failed acquisition cannot enter the merge body")
    elif kind == "merge-turn":
        with run_merge_turn._merge_wait(1, c.ctx):
            fail()
    elif kind == "executor":
        c.ctx.sleep = fail
        run_waits._executor_wait(1, c.ctx, 5)
    elif kind == "test-slot":
        monkeypatch.setattr(capacity, "hold_test_slot", failed_test_slot)
        with run_waits._test_slot(1, c.ctx):
            pytest.fail("A failed acquisition cannot enter the test body")
    elif kind == "context":
        with run_waits.in_phase(1, c.ctx, "checking"):
            fail()
    elif kind == "baseline":
        monkeypatch.setattr(run_baseline, "_capture_baseline", fail)
        run_baseline._capture_acceptance_baseline(1, c.ctx, "pytest", "base")
    elif kind == "local-baseline":
        monkeypatch.setattr(ci_local, "_capture_local_only", fail)
        ci_local.capture_local_only(1, c.ctx, None, "pytest", "base", [])
    else:
        with run_waits.in_phase(1, c.ctx, "ci"):
            fail()


@pytest.mark.parametrize("kind", INTERRUPTED_WAITS)
@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_interrupted_wait_restores_the_work_phase_into_a_free_slot(slot_case, monkeypatch, kind, exception):
    from foragent import capacity, run_waits

    c, error = slot_case, exception("original failure")
    before = c.store.get_run(1).phase
    assert capacity.phase_holds_slot(before)

    def fail(*args, **kwargs):
        run_waits._phase(1, c.ctx, "overlap-wait")
        c.clock[0] += timedelta(seconds=3)
        raise error

    with pytest.raises(exception) as caught:
        _interrupt_wait(c, monkeypatch, kind, fail)
    run = c.store.get_run(1)
    assert caught.value is error and c.sleeps == []
    assert run.phase == ("merging" if kind == "merge-lock" else before)
    assert run.waiting_until is None and capacity.machine_runs(c.store, c.ctx.now()) == 1


@pytest.mark.parametrize("source,failure", [("_try_flock", PermissionError("lock")),
                                            ("workload_budget", ValueError("budget"))], ids=["lock", "budget"])
@pytest.mark.parametrize("kind", INTERRUPTED_WAITS)
def test_interrupted_wait_keeps_its_exception_when_the_machine_lock_is_unusable(
    slot_case, monkeypatch, kind, source, failure,
):
    from foragent import capacity, run_waits

    c, error = slot_case, RuntimeError("original failure")

    def unusable(*args, **kwargs):
        raise failure

    def fail(*args, **kwargs):
        run_waits._phase(1, c.ctx, "overlap-wait")
        monkeypatch.setattr(capacity, source, unusable)
        raise error

    with pytest.raises(RuntimeError) as caught:
        _interrupt_wait(c, monkeypatch, kind, fail)
    assert caught.value is error
    assert c.store.get_run(1).phase in capacity.IDLE_RUN_PHASES


def test_interrupted_wait_stays_idle_while_a_kernel_hold_blocks_new_work(slot_case, monkeypatch):
    from foragent import capacity, run_waits

    c = slot_case
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "critical")

    def fail():
        raise RuntimeError("original failure")

    with pytest.raises(RuntimeError, match="original failure"):
        run_waits._credited_wait(1, c.ctx, fail, "overlap-wait")
    assert c.store.get_run(1).phase == "overlap-wait" and c.sleeps == []


@pytest.mark.parametrize("full", ["before-lock", "after-lock", "never"])
def test_merge_slot_wait_releases_the_lock_and_retries_before_entering(slot_case, monkeypatch, full):
    import fcntl

    from foragent import capacity, run_waits

    c = slot_case
    run_waits._phase(1, c.ctx, "ci")
    if full == "before-lock":
        c.occupy()
    lock, attempts, effects = c.store.merge_lock, [], []

    @contextmanager
    def acquire():
        assert c.store.get_run(1).phase == "merge-lock-wait"
        with lock():
            attempts.append(True)
            if full == "after-lock" and len(attempts) == 1:
                c.occupy()  # A new start claims the slot released while waiting for the merge lock.
            yield

    def sleep(seconds):
        # Check the kernel lock, not a self-reported lock flag.
        with (c.store.root / "merge.lock").open("a") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        assert c.store.get_run(1).phase in capacity.IDLE_RUN_PHASES
        assert capacity.machine_runs(c.store, c.ctx.now()) == 1 and effects == []
        c.sleeps.append(seconds)
        c.clock[0] += timedelta(seconds=seconds)
        c.other.finish_run(9, outcome="merged")

    monkeypatch.setattr(c.store, "merge_lock", acquire)
    monkeypatch.setattr(capacity, "time", SimpleNamespace(monotonic=c.ctx.monotonic, sleep=sleep))
    with _merge_lock(1, c.ctx):
        assert c.store.get_run(1).phase == "merging"
        assert capacity.machine_runs(c.store, c.ctx.now()) == 1
        effects.append("merged")
        c.clock[0] += timedelta(seconds=4)
        c.store.record_merge(1, c.ctx.now())
    assert effects == ["merged"] and len(attempts) == (2 if full == "after-lock" else 1)
    assert c.sleeps == ([] if full == "never" else [capacity.WORKLOAD_POLL_SECONDS])
    run = c.store.get_run(1)
    assert run.merge_lock_held_seconds == 4
    assert run.waited_seconds == sum(c.sleeps) and run.waiting_until is None
    c.store.finish_run(1, outcome="merged")
    assert capacity.machine_runs(c.store, c.ctx.now()) == 0


def test_successful_wait_inside_an_exception_handler_still_reacquires_capacity(slot_case, monkeypatch):
    from foragent import capacity, run_waits

    c = slot_case

    def acquire():
        c.occupy()
        c.clock[0] += timedelta(seconds=3)

    def sleep(seconds):
        assert c.store.get_run(1).phase == "overlap-wait"
        c.sleeps.append(seconds)
        c.clock[0] += timedelta(seconds=seconds)
        c.other.finish_run(9, outcome="merged")

    monkeypatch.setattr(capacity, "time", SimpleNamespace(monotonic=c.ctx.monotonic, sleep=sleep))
    try:
        raise RuntimeError("already handled")
    except RuntimeError:
        run_waits._credited_wait(1, c.ctx, acquire, "overlap-wait")
        assert c.store.get_run(1).phase == "reviewing"
        assert capacity.machine_runs(c.store, c.ctx.now()) == 1
    assert c.sleeps == [capacity.WORKLOAD_POLL_SECONDS]
    assert c.store.get_run(1).waited_seconds == 3 + sum(c.sleeps)


@pytest.mark.parametrize("operation", ["get_issue", "pr_checks", "merge_pr", "board"])
def test_github_limit_from_runner_stages_waits_without_followup_calls(run_case, monkeypatch, operation):
    c = run_case
    c.store.update_run(1, attempts=3, restarts=1)
    until = c.now[0] + timedelta(minutes=45)
    failed = False
    followups = []

    def limited(*args, **kwargs):
        nonlocal failed
        failed = True
        raise TrackerError("API rate limit exceeded (HTTP 403)",
                           stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    with monkeypatch.context() as patch:
        for method in ("get_issue", "pr_head_sha", "add_labels", "remove_labels", "comment"):
            original = getattr(c.tracker, method)

            def tracked(*args, _method=method, _original=original, **kwargs):
                if failed:
                    followups.append(_method)
                return _original(*args, **kwargs)

            patch.setattr(c.tracker, method, tracked)
        if operation == "board":
            c.ctx.config = replace(c.ctx.config, board="project")
            patch.setattr("foragent.board.set_status", limited)
        else:
            patch.setattr(c.tracker, operation, limited)
        assert run_issue(1, c.ctx) == "limit-wait"
        assert followups == []
    assert failed
    run = StateStore(c.store.root).get_run(1)
    assert run.waiting_until == until.isoformat() and run.outcome == "limit-wait"
    assert (run.attempts, run.restarts) == (3, 1)
    assert c.store.load_flag(1) is None
    assert not c.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_runner_launched_during_persisted_github_wait_stays_offline(run_case, monkeypatch):
    c = run_case
    until = c.now[0] + timedelta(minutes=10)
    c.store.pause_github(until)
    with monkeypatch.context() as patch:
        patch.setattr(c.tracker, "get_issue", lambda *args: pytest.fail("waiting runner must stay offline"))
        assert run_issue(1, c.ctx) == "limit-wait"
    assert c.executor.calls == []
    assert c.store.get_run(1).waiting_until == until.isoformat()


@pytest.mark.parametrize("expired", [False, True])
def test_session_timeout_and_exhausted_budget(run_case, expired):
    c = run_case
    if expired:
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)
    else:
        c.executor.timed_out = True
    assert run_issue(1, c.ctx) == "timeout"
    assert c.store.load_flag(1).reason == Reason.TIMEOUT
    assert "Too big for one session; split it in order" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []
    assert len(c.executor.calls) == (0 if expired else 1)


def test_merge_lock_wait_credits_elapsed_time_and_keeps_active_budget(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    merge_lock = c.store.merge_lock
    entries = []

    @contextmanager
    def delayed_lock():
        waiting = c.store.get_run(1)
        assert waiting.waiting_until is not None
        c.now[0] += timedelta(seconds=300)
        with merge_lock():
            entries.append(True)
            yield

    monkeypatch.setattr(c.store, "merge_lock", delayed_lock)
    assert run_issue(1, c.ctx) == "merged"
    run = c.store.get_run(1)
    assert entries == [True] and run.waited_seconds == 300 and run.waiting_until is None
    assert c.store.load_flag(1) is None


@pytest.mark.parametrize("prior_backoff", [False, True])
def test_merge_lock_wait_does_not_consume_conflict_executor_backoff_budget(conflicting_lanes, monkeypatch, prior_backoff):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    results = [("done", "normal"), ("503", "transient"), ("done", "normal")]
    if prior_backoff:
        c.executor.steps.insert(0, {})
        results.insert(0, ("503", "transient"))
    scripted_results(c, monkeypatch, results)
    merge_lock = c.store.merge_lock
    waits = []
    lock_waits = []

    @contextmanager
    def delayed_lock():
        lock_waits.append(c.now[0])
        if len(lock_waits) == 1:
            c.move_target()
        c.now[0] += timedelta(minutes=31)
        with merge_lock():
            yield

    def checks(pr):
        head = c.tracker.pr_head_sha(pr)
        c.ci_heads.append(head)
        return "pending" if head != c.ci_heads[0] and c.ci_heads.count(head) == 1 else "pass"

    def sleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    monkeypatch.setattr(c.store, "merge_lock", delayed_lock)
    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120] * (2 if prior_backoff else 1) + [30]
    run = c.store.get_run(1)
    assert run.attempts == 1 and run.executor_waited_seconds == 120 * (2 if prior_backoff else 1)
    assert len(lock_waits) == 2  # Discover the conflict, then queue the verified resolution.
    assert run.waited_seconds == len(lock_waits) * 31 * 60 + run.executor_waited_seconds
    assert [call["timeout_s"] for call in c.executor.calls] == [60] * (4 if prior_backoff else 3)
    assert c.store.load_flag(1) is None


def test_backoff_oversleep_preserves_runtime_for_next_executor(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    c.executor.steps = [{}, {"feature.txt": "done"}]
    scripted_results(c, monkeypatch, [("HTTP 503", "transient"), ("done", "normal")])
    waits = []

    def oversleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=300)

    c.ctx.sleep = oversleep
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120]
    assert [call["timeout_s"] for call in c.executor.calls] == [60, 60]
    assert c.store.get_run(1).waited_seconds == 300
    assert c.store.get_run(1).executor_waited_seconds == 300


def test_merge_lock_heartbeat_records_wait_and_clears_it_after_acquisition(tmp_path, monkeypatch):
    from foragent.config import Config

    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=10, executor_waited_seconds=5)
    ctx = SimpleNamespace(store=store, now=lambda: now[0], config=Config(),
                          monotonic=lambda: (now[0] - BASE_TIME).total_seconds())
    monkeypatch.setattr("foragent.capacity.machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr("foragent.capacity.kernel_pressure", lambda: "normal")

    def acquire(execute, poll):
        now[0] += timedelta(minutes=20)
        poll()
        run = store.get_run(1)
        assert run.waited_seconds == 1210 and run.last_progress_at == now[0].isoformat()
        assert run.executor_waited_seconds == 5
        assert run.waiting_until == (now[0] + timedelta(seconds=60)).isoformat()
        return execute()

    monkeypatch.setattr("foragent.runner.poll_executor", acquire)
    with _merge_lock(1, ctx):
        assert store.get_run(1).waiting_until is None
    assert store.get_run(1).waited_seconds == 1210
    assert store.get_run(1).executor_waited_seconds == 5


@pytest.mark.parametrize("elapsed,interrupted", [(70, True), (300, True), (300, False)])
def test_backoff_records_monotonic_elapsed_even_if_interrupted_or_wall_clock_moves(tmp_path, elapsed, interrupted):
    wall = [BASE_TIME]
    mono = [100.0]
    store = StateStore(tmp_path / "state", now=lambda: wall[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=10, executor_waited_seconds=5)

    def sleep(seconds):
        assert store.get_run(1).waited_seconds == 130
        assert store.get_run(1).executor_waited_seconds == 125
        mono[0] += elapsed
        wall[0] -= timedelta(hours=1)
        if interrupted:
            raise SystemExit(1)

    ctx = SimpleNamespace(store=store, now=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep)
    if interrupted:
        with pytest.raises(SystemExit):
            _executor_wait(1, ctx, 120)
    else:
        _executor_wait(1, ctx, 120)
    run = store.get_run(1)
    assert run.waited_seconds == 10 + elapsed
    assert run.executor_waited_seconds == 5 + elapsed
    assert run.waiting_until is None and run.last_progress_at == wall[0].isoformat()


@pytest.mark.parametrize("queued", [True, False])
def test_test_slot_wait_is_a_phase_only_when_the_run_queues(tmp_path, monkeypatch, queued):
    import json

    from foragent import capacity
    from foragent.run_waits import _test_slot

    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, phase="checking", phase_started_at=BASE_TIME.isoformat())
    ctx = SimpleNamespace(store=store, now=lambda: now[0], config=SimpleNamespace(test_slots=1))

    @contextmanager
    def hold(slots, *, waiting):
        if queued:
            now[0] += timedelta(seconds=5)
            waiting()
            assert store.get_run(1).phase == "test-slot-wait"
            now[0] += timedelta(seconds=30)
            waiting()  # A refresh while still queued opens no new segment.
            now[0] += timedelta(seconds=10)
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", hold)
    with _test_slot(1, ctx):
        assert store.get_run(1).phase == "checking"
    journal = store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    # The queue segment starts when the run asks for a slot, so checking closes at 0 seconds.
    expected = [("checking -> test-slot-wait", 0), ("test-slot-wait -> checking", 45)] if queued else []
    assert [(event["summary"], event["seconds"]) for event in events] == expected


@pytest.mark.parametrize("transition", ["phase", "context", "restore", "close"])
@pytest.mark.parametrize("idle", ["merge-turn-wait", "merge-lock-wait", "ci", "overlap-wait"])
def test_idle_exit_queues_before_restoring_work_and_credits_queue_time(tmp_path, monkeypatch, transition, idle):
    import os

    from foragent import capacity, run_waits
    from foragent.config import Config

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "own", now=lambda: clock[0])
    other = StateStore(tmp_path / "other", now=lambda: clock[0])
    store.start_run(1, branch="b", worktree="w", pid=os.getpid())
    store.update_run(1, phase="reviewing", phase_work_id="review-head")
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: clock[0],
                          monotonic=lambda: (clock[0] - BASE_TIME).total_seconds())
    identity = run_waits.phase_identity(store.get_run(1))
    wait = run_waits.WaitPhase(1, ctx, idle)
    wait.open()
    other.start_run(2, branch="b", worktree="w", pid=os.getpid())
    capacity.write_state(store, 1, clock[0])
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    sleeps = []

    def sleep(seconds):
        assert store.get_run(1).phase == idle
        assert capacity.machine_runs(store, clock[0]) == 1
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        other.finish_run(2, outcome="merged")

    monkeypatch.setattr(capacity, "time", SimpleNamespace(sleep=sleep, monotonic=ctx.monotonic))

    def working():
        run = store.get_run(1)
        assert run.phase == "reviewing" and capacity.machine_runs(store, clock[0]) == 1
        assert run.phase_started_at == clock[0].isoformat()
        if transition in {"restore", "close"}:
            assert run_waits.phase_identity(run) == identity

    if transition == "phase":
        run_waits._phase(1, ctx, "reviewing")
        working()
    elif transition == "context":
        with run_waits.in_phase(1, ctx, "reviewing"):
            working()
        assert store.get_run(1).phase == idle
    elif transition == "restore":
        run_waits.restore_phase(1, ctx, identity)
        working()
    else:
        wait.close()
        working()
    run = store.get_run(1)
    assert sleeps == [capacity.WORKLOAD_POLL_SECONDS]
    assert run.waited_seconds == sum(sleeps) and run.waiting_until is None
    store.finish_run(1, outcome="merged")
    assert capacity.machine_runs(store, clock[0]) == 0


@pytest.mark.parametrize("transition", ["phase", "context", "restore", "close", "merge-lock"])
@pytest.mark.parametrize("hold", ["full", "critical", "overload"])
def test_idle_exit_credits_long_queues_without_spending_remaining_runtime(slot_case, monkeypatch, transition, hold):
    from foragent import capacity, runner, run_waits
    from foragent.config import Config

    c = slot_case
    c.ctx.config = Config(max_runtime_minutes=1)
    identity = run_waits.phase_identity(c.store.get_run(1))
    wait = run_waits.WaitPhase(1, c.ctx, "ci")
    wait.open()
    c.clock[0] += timedelta(seconds=57)
    assert runner._remaining(1, c.ctx) == 3
    released = []
    if hold == "full":
        c.occupy()
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "critical" if hold == "critical" and not released else "normal")
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 2.0 if hold == "overload" and not released else 0.5))
    monkeypatch.setattr(runner, "_timeout", lambda *args: pytest.fail("Paused queue time cannot exhaust runtime"))

    def sleep(seconds):
        assert c.store.get_run(1).phase == "ci"
        assert runner._remaining(1, c.ctx) == 3
        assert capacity.machine_runs(c.store, c.ctx.now()) == (1 if hold == "full" else 0)
        c.sleeps.append(seconds)
        c.clock[0] += timedelta(seconds=seconds)
        if sum(c.sleeps) >= 10:
            released.append(True)
            if hold == "full":
                c.other.finish_run(9, outcome="merged")

    monkeypatch.setattr(capacity, "time", SimpleNamespace(monotonic=c.ctx.monotonic, sleep=sleep))

    def work():
        assert released and sum(c.sleeps) == 10
        assert capacity.machine_runs(c.store, c.ctx.now()) == 1
        assert runner._remaining(1, c.ctx) == 3
        c.clock[0] += timedelta(seconds=3)
        assert runner._remaining(1, c.ctx) == 0, "Real work still spends the original runtime budget"

    if transition == "phase":
        run_waits._phase(1, c.ctx, "implementing")
        work()
    elif transition == "context":
        with run_waits.in_phase(1, c.ctx, "checking"):
            work()
    elif transition == "restore":
        run_waits.restore_phase(1, c.ctx, identity)
        work()
    elif transition == "close":
        wait.close()
        work()
    else:
        with _merge_lock(1, c.ctx):
            work()
    run = c.store.get_run(1)
    assert run.waited_seconds == 10 and run.waiting_until is None
    c.store.finish_run(1, outcome="merged")
    assert capacity.machine_runs(c.store, c.ctx.now()) == 0

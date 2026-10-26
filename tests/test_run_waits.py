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
    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=10, executor_waited_seconds=5)
    ctx = SimpleNamespace(store=store, now=lambda: now[0], monotonic=lambda: (now[0] - BASE_TIME).total_seconds())

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
    expected = [("checking -> test-slot-wait", 5), ("test-slot-wait -> checking", 40)] if queued else []
    assert [(event["summary"], event["seconds"]) for event in events] == expected

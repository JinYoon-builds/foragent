"""Boundary tests for `fa orch stop` (D95 order) beyond the locked acceptance files.

Processes are a fake table only: no test sends a real signal.
"""

from datetime import UTC, datetime, timedelta
import json
import os
import threading
from types import SimpleNamespace

import pytest

from foragent import orch, orch_stop, processes, scheduler
from foragent.git import GitError
from foragent.processes import ProcessIdentity, ProcessTable
from foragent.run_publish import _merge_unless_stopped
from foragent.scheduler_stop import stop_target_refusal
from foragent.state import Run, StateStore

COMMAND, RUNNER = 91003, 42000


def _run(**fields):
    base = dict(number=1, branch="foragent/issue-1", worktree="/nowhere", pid=RUNNER,
                started_at="2026-10-26T00:00:00+00:00", last_activity_at="2026-10-26T00:00:00+00:00",
                last_progress_at="2026-10-26T00:00:00+00:00",
                status="running", executor_pgid=None,
                process_identities=[{"pid": RUNNER, "start_time": 5.0}])
    return Run(**{**base, **fields})


@pytest.mark.parametrize("field", ["pid", "executor_pgid"])
def test_unreadable_recorded_pid_is_refused(monkeypatch, field):
    # Unreadable rows cannot prove ownership; a missing row would wrongly pass.
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable((), unreadable={RUNNER}))
    refusal = stop_target_refusal(_run(**{"pid": None, field: RUNNER}))
    assert refusal is not None and "cannot be read" in refusal


@pytest.mark.parametrize(("pid", "identities"), [
    (None, []),  # launched but never recorded: nothing proves which process to stop
    (RUNNER, []),  # a numeric PID without its recorded birth is not ownership
    (RUNNER, [{"pid": 41999, "start_time": 5.0}]),
])
def test_runner_without_a_recorded_identity_is_refused(monkeypatch, pid, identities):
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable(()))
    refusal = stop_target_refusal(_run(pid=pid, process_identities=identities))
    assert refusal is not None and "ownership cannot be verified" in refusal


def test_recorded_live_owned_pid_passes(monkeypatch):
    row = ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0)
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable((row,)))
    assert stop_target_refusal(_run()) is None


class _Os:
    """processes.os with a fake PID for the stop command; nothing else changes."""

    def __init__(self, pid):
        self.pid = pid

    def getpid(self):
        return self.pid

    def __getattr__(self, name):
        return getattr(os, name)


class _Table:
    """A fake process table: the stop command, the runner and anything a test adds."""

    def __init__(self):
        self.rows = {COMMAND: ProcessIdentity(COMMAND, 1, COMMAND, COMMAND, 1.0),
                     RUNNER: ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0)}

    def __call__(self):
        return ProcessTable(tuple(self.rows.values()))


class _Tracker:
    def __init__(self):
        self.calls, self.pr, self.fail = [], None, set()
        self.issue = SimpleNamespace(state="open", title="Locale", labels={"approved", "running"})

    def _maybe_fail(self, step):
        if step in self.fail:
            raise RuntimeError(f"GitHub failed: {step}")

    def get_issue(self, n):
        return self.issue

    def find_pr(self, head):
        return self.pr

    def close_pr(self, pr, comment=None):
        self.calls.append(("close", pr))
        self.pr = None

    def comment(self, n, body):
        self.calls.append(("comment", n))

    def add_labels(self, n, labels):
        self._maybe_fail(f"add {labels[0]}")
        self.calls.append(("add", n, tuple(labels)))

    def remove_labels(self, n, labels):
        self._maybe_fail(f"remove {labels[0]}")
        self.calls.append(("remove", n, tuple(labels)))


READY = [("add", 1, ("ready",)), ("remove", 1, ("blocked",)), ("remove", 1, ("needs-human",)),
         ("remove", 1, ("running",))]


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 27, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    table = _Table()
    monkeypatch.setattr(processes, "list_processes", table)
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    monkeypatch.setattr(processes, "os", _Os(COMMAND))
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *args: None)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    store.update_run(1, pid=RUNNER, process_identities=[{"pid": RUNNER, "start_time": 5.0}])
    tracker = _Tracker()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=tracker, store=store, config=None,
                          git=SimpleNamespace(), now=lambda: clock[0], sleep=sleep)
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(orch, "kick_tick", lambda root: None)
    monkeypatch.setattr("foragent.scheduler_stop.stop_target_refusal", lambda run: None)
    return SimpleNamespace(ctx=ctx, store=store, tracker=tracker, table=table, clock=clock, sleeps=sleeps)


def _stop_events(store, kind="stop"):
    path = store.root / "orch-events.jsonl"
    return [event for event in map(json.loads, path.read_text().splitlines()) if event["kind"] == kind] \
        if path.exists() else []


def _killer(case, *, dies=True):
    calls = []

    def kill(ctx, number, report, **kwargs):
        calls.append(kwargs)
        assert orch_stop.read_request(ctx.store, number) is not None, "The request must be durable before the kill"
        if dies:
            case.table.rows.pop(RUNNER, None)
        return True

    return calls, kill


def test_stop_writes_the_request_first_then_finishes_after_the_tree_is_dead(case, monkeypatch):
    calls, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    summary = orch.stop(case.ctx, 1, "reprioritize")
    assert calls == [{"outcome": "stopped"}]
    run = case.store.get_run(1)
    assert (run.status, run.outcome) == ("finished", "stopped")
    assert "nothing to save" in summary and "reprioritize" in summary
    [event] = _stop_events(case.store)
    assert "nothing to save" in event["summary"] and "reprioritize" in event["summary"]
    assert [call for call in case.tracker.calls if call[0] != "comment"] == READY
    assert orch_stop.read_request(case.store, 1) is None, "The request is deleted last"


def test_a_tree_still_alive_after_60_seconds_exits_with_the_request_kept(case, monkeypatch):
    calls, kill = _killer(case, dies=False)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="wait-for-tree"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert len(case.sleeps) == orch_stop.TREE_WAIT_S and set(case.sleeps) == {1}
    assert orch_stop.read_request(case.store, 1) is not None
    assert case.store.get_run(1).status == "running" and case.tracker.calls == []


def test_a_descendant_found_after_the_kill_keeps_the_tree_alive(case):
    case.table.rows.pop(RUNNER)
    case.table.rows[43000] = ProcessIdentity(43000, RUNNER, 43000, 43000, 9.0)
    run = case.store.get_run(1)
    assert orch_stop.tree_alive(run) is False, "A child of a PID no longer in the tree is not this tree"
    case.table.rows[RUNNER] = ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0)
    assert orch_stop.tree_alive(run)
    case.table.rows.pop(RUNNER)
    case.table.rows[43000] = ProcessIdentity(43000, 1, 43000, 43000, 9.0)
    case.store.update_run(1, process_identities=[{"pid": RUNNER, "start_time": 5.0},
                                                 {"pid": 43000, "start_time": 9.0}])
    assert orch_stop.tree_alive(case.store.get_run(1)), "A recorded descendant reparented to init still lives"


@pytest.mark.parametrize("step", [1, 10])
def test_a_busy_worktree_lock_after_the_kill_exits_on_the_5_minute_deadline_with_the_request_kept(
    case, monkeypatch, step,
):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    held, release = threading.Event(), threading.Event()

    def holder():  # Not the runner: its lock dies with it. Another holder never blocks the kill.
        with case.store.worktree_lock(1):
            held.set()
            release.wait(10)

    def sleep(seconds):  # A late wakeup: each 1 s sleep may take longer on the clock.
        case.sleeps.append(seconds)
        case.clock[0] += timedelta(seconds=step)

    case.ctx.sleep = sleep
    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(10)
    start = case.clock[0]
    try:
        with pytest.raises(orch.OrchError, match="worktree-lock"):
            orch.stop(case.ctx, 1, "reprioritize")
    finally:
        release.set()
        thread.join()
    assert case.clock[0] - start <= timedelta(seconds=orch_stop.LOCK_WAIT_S + step)
    assert len(case.sleeps) == orch_stop.LOCK_WAIT_S // step, "The deadline is elapsed time, not a count"
    assert orch_stop.read_request(case.store, 1) is not None, "After the kill the request stays for the tick"
    assert case.store.get_run(1).status == "running" and case.tracker.calls == []


def test_a_kill_inside_a_checkpointed_step_saves_the_checkpointed_work(case, monkeypatch):
    from foragent import orch_stop_checkpoint

    state = {"head": "src000", "tree": "tree000", "branch": "foragent/issue-1", "branch_tip": "src000"}
    # The runner entered a destructive step (rebuild then re-apply) and was killed inside it.
    cut = orch_stop_checkpoint.checkpoint(case.store, 1, state)  # Kept: a dead runner never unwinds it.
    cut.__enter__()
    order = []
    worktree = _Worktree()
    _saved_worktree(case, monkeypatch, worktree)
    monkeypatch.setattr(orch_stop_checkpoint, "restore", lambda ctx, path, cut: order.append(("restore", cut)))
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(case.ctx, 1, "reprioritize")
    assert order == [("restore", {"started_at": case.store.get_run(1).started_at, **state})]
    assert orch_stop_checkpoint.read_checkpoint(case.store, 1) is None


def test_a_merge_call_in_flight_completes_before_the_kill_and_the_stop_reports_it(case, monkeypatch):
    from foragent import orch_stop_checkpoint

    marker = orch_stop_checkpoint.merging(case.store, 1)
    marker.__enter__()  # The runner called merge before the request.

    def sleep(seconds):
        case.clock[0] += timedelta(seconds=seconds)
        case.store.record_merge(1, case.clock[0])  # The merge call returns.
        case.store.finish_run(1, outcome="merged")
        marker.__exit__(None, None, None)
        case.table.rows.pop(RUNNER, None)

    case.ctx.sleep = sleep
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("A merge in flight is never cut"))
    assert "runner merged" in orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is None and case.tracker.calls == []


def test_an_unreadable_scan_before_the_kill_removes_the_request(case, monkeypatch):
    def unreadable():
        raise PermissionError("ps denied")

    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("No kill after a failed scan"))
    monkeypatch.setattr(processes, "list_processes", unreadable)
    monkeypatch.setattr(orch_stop, "write_request", lambda store, run, why, now, previous=None: orch_stop._write(
        store, record := {"number": 1, "started_at": run.started_at, "why": why, "requested_at": now.isoformat(),
                          "pid": COMMAND, "start_time": 1.0}) or record)
    with pytest.raises(orch.OrchError, match="before the kill.*nothing was changed"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is None


def test_a_failed_kill_keeps_the_request_for_the_tick(case, monkeypatch):
    monkeypatch.setattr(scheduler, "_kill_run", lambda *args, **kwargs: False)
    with pytest.raises(orch.OrchError, match="step kill"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is not None


def test_a_failed_request_write_changes_nothing(case, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(case.store, "_atomic_write", broken)
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("No kill without a request"))
    with pytest.raises(OSError):
        orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is None and case.store.get_run(1).status == "running"


def test_a_second_stop_refuses_while_the_first_command_lives_and_takes_over_once_it_died(case, monkeypatch):
    run = case.store.get_run(1)
    orch_stop.write_request(case.store, run, "first", case.clock[0])
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("A live stop owns the run"))
    with pytest.raises(orch.OrchError, match="already stopping"):
        orch.stop(case.ctx, 1, "second")
    case.table.rows.pop(COMMAND)  # The first stop command died.
    processes.os.pid = 91010
    case.table.rows[91010] = ProcessIdentity(91010, 1, 91010, 91010, 2.0)
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    assert "second" in orch.stop(case.ctx, 1, "second")
    assert case.store.get_run(1).outcome == "stopped"


def test_a_batch_run_is_refused_without_a_request(case, monkeypatch):
    case.store.start_run(2, branch="foragent/batch-7", worktree=case.ctx.repo_root / "batch")
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("Batch runs are not stopped"))
    with pytest.raises(orch.OrchError, match="batch"):
        orch.stop(case.ctx, 2, "reprioritize")
    assert orch_stop.read_request(case.store, 2) is None


def test_a_runner_merge_before_the_tree_died_changes_nothing_else(case, monkeypatch):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    case.store.record_merge(1, case.clock[0])
    summary = orch.stop(case.ctx, 1, "reprioritize")
    assert "runner merged" in summary
    assert orch_stop.read_request(case.store, 1) is None
    assert case.store.get_run(1).status == "running" and case.tracker.calls == [] and not _stop_events(case.store)


def test_a_closed_issue_changes_nothing_else(case, monkeypatch):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    case.tracker.issue.state = "closed"
    assert "closed" in orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is None and case.tracker.calls == []


class _Worktree:
    def __init__(self, push_error=None):
        self.push_error, self.pushes, self.head = push_error, [], "base000000000000"
        self.published = "base000000000000"  # origin's foragent/issue-1

    def head_sha(self):
        return self.head

    def _call(self, args):
        if args[0] == "push":
            self.pushes.append(args)
            if self.push_error:
                raise GitError(self.push_error)
            self.published = self.head
        if args[:2] == ["rev-list", "--count"]:
            return "0" if self.head == args[-1] else "1"
        return ""


def _saved_worktree(case, monkeypatch, worktree):
    path = case.ctx.repo_root / "wt"
    path.mkdir()
    removed = []
    case.ctx.git = SimpleNamespace(at=lambda root: worktree, remove_worktree=lambda root: removed.append(root),
                                   remote_branch_sha=lambda branch: worktree.published)
    def preserve(n, ctx, git, reason, **kwargs):
        if git.head == "base000000000000" and reason == "stopped: reprioritize":
            git.head = "abc123def4567890"  # The WIP commit.
            return "saved"
        return "clean"

    monkeypatch.setattr("foragent.run_publish._preserve_failed_implementation", preserve)
    return removed


def test_a_rejected_push_keeps_the_commit_and_the_request_and_a_retry_finishes(case, monkeypatch):
    worktree = _Worktree("! [rejected] (fetch first)")
    removed = _saved_worktree(case, monkeypatch, worktree)
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="push.*abc123def4567890.*kept locally"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert worktree.pushes == [["push", "origin", "HEAD:refs/heads/foragent/issue-1"]], "Never a forced push"
    assert removed == [] and orch_stop.read_request(case.store, 1)["saved"] == "abc123def4567890"
    assert case.store.get_run(1).status == "running" and not _stop_events(case.store)
    # The stop command died; the next tick is the fallback owner and finishes it.
    case.table.rows.pop(COMMAND)
    worktree.push_error = None
    report = scheduler.TickReport()
    assert orch_stop.recover_requests(case.ctx, report) == {1}
    [event] = _stop_events(case.store)
    assert "abc123def456" in event["summary"] and "reprioritize" in event["summary"]
    assert case.store.get_run(1).outcome == "stopped" and removed
    assert orch_stop.read_request(case.store, 1) is None


def test_the_stop_closes_this_issues_open_pr_before_returning_it_to_ready(case, monkeypatch):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    case.tracker.pr = 17
    assert "closed PR #17" in orch.stop(case.ctx, 1, "reprioritize")
    assert case.tracker.calls[0] == ("comment", 1) and case.tracker.calls[1] == ("close", 17)


def test_a_failed_label_step_keeps_the_request_and_the_tick_finishes_once(case, monkeypatch):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    case.tracker.fail = {"remove running"}
    with pytest.raises(orch.OrchError, match="labels"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert orch_stop.read_request(case.store, 1) is not None and case.store.get_run(1).outcome == "stopped"
    case.table.rows.pop(COMMAND)
    case.tracker.fail = set()
    orch_stop.recover_requests(case.ctx, scheduler.TickReport())
    assert orch_stop.read_request(case.store, 1) is None
    assert len(_stop_events(case.store)) == 1, "The stop event is written once"
    assert ("remove", 1, ("running",)) in case.tracker.calls


def test_the_tick_leaves_a_request_with_a_live_command_alone(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    assert orch_stop.recover_requests(case.ctx, scheduler.TickReport()) == {1}
    assert orch_stop.read_request(case.store, 1) is not None and case.store.get_run(1).status == "running"


def test_a_dead_command_with_a_live_runner_expires_the_request(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    report = scheduler.TickReport()
    assert orch_stop.recover_requests(case.ctx, report) == set(), "Normal stall and runtime kills apply again"
    [event] = _stop_events(case.store, "stop-request-expired")
    assert event["summary"] == "Removed a stale stop request for #1"
    assert orch_stop.read_request(case.store, 1) is None and case.store.get_run(1).status == "running"


def test_a_request_for_a_run_finished_otherwise_expires_after_10_minutes(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    case.store.finish_run(1, outcome="retries-exceeded")
    case.clock[0] += timedelta(minutes=9)
    orch_stop.recover_requests(case.ctx, scheduler.TickReport())
    assert orch_stop.read_request(case.store, 1) is not None
    case.clock[0] += timedelta(minutes=1)
    orch_stop.recover_requests(case.ctx, scheduler.TickReport())
    assert orch_stop.read_request(case.store, 1) is None
    assert len(_stop_events(case.store, "stop-request-expired")) == 1 and not _stop_events(case.store)


def test_a_stopped_run_with_its_request_left_is_finished_by_the_tick(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    case.table.rows.pop(RUNNER)
    case.store.finish_run(1, outcome="stopped", detail="stopped: why")
    orch_stop.recover_requests(case.ctx, scheduler.TickReport())
    assert orch_stop.read_request(case.store, 1) is None
    assert [call for call in case.tracker.calls if call[0] != "comment"] == READY
    assert not _stop_events(case.store, "stop-request-expired")


def test_other_kills_skip_a_run_with_a_stop_request(case, monkeypatch):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    monkeypatch.setattr("foragent.scheduler_recovery._stop_run_processes",
                        lambda *a, **k: pytest.fail("A stall or runtime kill reached a stopping run"))
    assert scheduler._kill_run(case.ctx, 1, scheduler.TickReport(), outcome="stalled-restart",
                               count_stall=True) is False
    assert case.store.get_run(1).restarts == 0


def test_the_stop_kill_only_terminates_and_an_already_gone_tree_counts(case, monkeypatch):
    monkeypatch.setattr("foragent.scheduler_recovery._stop_run_processes", lambda *a, **k: "gone")
    monkeypatch.setattr("foragent.scheduler_recovery._finish_dead_run",
                        lambda *a: pytest.fail("The stop finishes the run, not the kill"))
    assert scheduler._kill_run(case.ctx, 1, scheduler.TickReport(), outcome="stopped") is True
    assert case.store.get_run(1).status == "running"


def test_a_merge_rechecks_the_request_under_the_worktree_lock(case):
    merged = []
    _merge_unless_stopped(case.ctx, 1, lambda: merged.append(1))
    assert merged == [1] and orch_stop.runner_merged(case.store, case.store.get_run(1))
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.ctx.repo_root / "wt2")
    orch_stop.write_request(case.store, case.store.get_run(2), "why", case.clock[0])
    with pytest.raises(orch_stop.StopRequested):
        _merge_unless_stopped(case.ctx, 2, lambda: pytest.fail("No merge after a stop request"))


def test_a_request_for_an_older_run_does_not_stop_the_current_one(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.store.finish_run(1, outcome="stopped")
    case.clock[0] += timedelta(minutes=1)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.ctx.repo_root / "wt")
    assert orch_stop.stop_request(case.store, 1) is None and not orch_stop.stop_owns(case.store, 1)


def test_the_worktree_lock_reenters_on_its_thread_and_excludes_others(case):
    store = case.store
    seen = []

    def other():
        with store.try_worktree_lock(1) as held:
            seen.append(held)

    with store.worktree_lock(1):
        with store.worktree_lock(1):  # A nested caller (preservation, removal) reenters.
            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
    assert seen == [False]
    with store.try_worktree_lock(1) as held:
        assert held


def _locked_from_another_thread(store, n=1):
    seen = []

    def other():
        with store.try_worktree_lock(n) as held:
            seen.append(not held)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    return seen[0]


def test_replay_source_restoration_holds_the_worktree_lock_from_recreation_to_read_tree(case, monkeypatch):
    from foragent import run_conflicts
    from foragent.run_attempt import PatchReplayError

    store, seen = case.store, []
    monkeypatch.setattr(run_conflicts, "_inject_lane_skills", lambda *a: seen.append(("skills", _locked_from_another_thread(store))))

    class Git:
        def ref_sha(self, ref):
            return "sha"

        def head_sha(self):
            return "dest"

        def staged_tree_sha(self):
            return "dest-tree"

        def remove_worktree(self, path):
            with store.worktree_lock(1):  # Main's removal takes the lock itself; it reenters here.
                seen.append(("remove", _locked_from_another_thread(store)))

        def add_worktree(self, *args):
            seen.append(("add", _locked_from_another_thread(store)))

        def _call(self, args):
            if args[0] != "symbolic-ref":
                seen.append((args[0], _locked_from_another_thread(store)))
            return ""

    git = Git()
    attempt = SimpleNamespace(n=1, ctx=SimpleNamespace(store=store, git=git), git=git, worktree=case.ctx.repo_root / "wt",
                              start_ref="old", branch="foragent/issue-1", target_branch="batch-1", log=case.ctx.repo_root / "log")
    error = PatchReplayError("conflict", own_paths=[], source_head="head", source_tree="tree", source_target="main")
    from foragent import orch_stop_checkpoint

    monkeypatch.setattr(run_conflicts, "_inject_lane_skills", lambda *a: seen.append(
        ("skills", _locked_from_another_thread(store), orch_stop_checkpoint.read_checkpoint(store, 1)["head"])))
    run_conflicts._restore_replay_source(attempt, error)
    assert seen == [("remove", True), ("add", True), ("skills", True, "head"), ("read-tree", True)], \
        "The source, not the half-built worktree, is checkpointed for the whole step"
    assert not _locked_from_another_thread(store)
    assert orch_stop_checkpoint.read_checkpoint(store, 1) is None, "A finished step leaves no checkpoint"


def test_a_takeover_keeps_the_save_checkpoint_and_pushes_the_kept_wip(case, monkeypatch):
    worktree = _Worktree("! [rejected] (fetch first)")
    _saved_worktree(case, monkeypatch, worktree)
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="push"):
        orch.stop(case.ctx, 1, "reprioritize")
    case.table.rows.pop(COMMAND)  # The first stop command died; a second one takes over.
    processes.os.pid = 91010
    case.table.rows[91010] = ProcessIdentity(91010, 1, 91010, 91010, 2.0)
    worktree.push_error = None
    summary = orch.stop(case.ctx, 1, "reprioritize")
    assert "abc123def456" in summary and "nothing to save" not in summary
    assert len(worktree.pushes) == 2, "The kept WIP must still be pushed"


def test_unpublished_commits_made_before_the_save_are_pushed_not_called_nothing(case, monkeypatch):
    worktree = _Worktree()
    _saved_worktree(case, monkeypatch, worktree)
    worktree.head = "abc123def4567890"  # The run committed this itself (or a save died after committing).
    monkeypatch.setattr("foragent.run_publish._preserve_failed_implementation", lambda *a, **k: "clean")
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    summary = orch.stop(case.ctx, 1, "reprioritize")
    assert "abc123def456" in summary and "nothing to save" not in summary and worktree.pushes


def test_a_published_branch_with_nothing_new_is_nothing_to_save(case, monkeypatch):
    worktree = _Worktree()
    _saved_worktree(case, monkeypatch, worktree)
    monkeypatch.setattr("foragent.run_publish._preserve_failed_implementation", lambda *a, **k: "clean")
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    assert "nothing to save" in orch.stop(case.ctx, 1, "reprioritize") and not worktree.pushes


def test_a_leaderless_executor_group_and_a_reparented_descendant_keep_the_tree_alive(case):
    case.store.update_run(1, executor_pgid=42001, process_identities=[
        {"pid": RUNNER, "start_time": 5.0}, {"pid": 42001, "start_time": 6.0}])
    run = case.store.get_run(1)
    case.table.rows.pop(RUNNER)
    case.table.rows[42005] = ProcessIdentity(42005, 1, 42001, 42001, 7.0)  # Unrecorded group member.
    assert orch_stop.tree_alive(run), "The group outlives its leader"
    case.table.rows.pop(42005)
    case.table.rows[42006] = ProcessIdentity(42006, 42002, 42006, 42006, 8.0)
    case.table.rows[42002] = ProcessIdentity(42002, 42001, 42002, 42002, 7.5)
    case.store.update_run(1, process_identities=[{"pid": 42002, "start_time": 7.5}])
    seen = {}
    assert orch_stop.tree_alive(case.store.get_run(1), seen) and 42006 in seen
    case.table.rows.pop(42002)
    case.table.rows[42006] = ProcessIdentity(42006, 1, 42006, 42006, 8.0)  # Reparented after its parent died.
    assert orch_stop.tree_alive(case.store.get_run(1), seen)
    assert not orch_stop.tree_alive(case.store.get_run(1)), "Without the earlier scan it would look dead"


def test_a_finished_stopped_run_with_a_live_process_is_not_finished_or_taken_over(case, monkeypatch):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    case.store.finish_run(1, outcome="stopped")
    assert orch_stop.recover_requests(case.ctx, scheduler.TickReport()) == {1}
    assert orch_stop.read_request(case.store, 1) is not None and case.tracker.calls == []
    processes.os.pid = 91010
    case.table.rows[91010] = ProcessIdentity(91010, 1, 91010, 91010, 2.0)
    with pytest.raises(orch.OrchError, match="wait-for-tree"):
        orch.stop(case.ctx, 1, "again")
    assert case.tracker.calls == []


def test_a_tick_leaves_a_request_another_command_is_claiming(case):
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    held, release = threading.Event(), threading.Event()

    def claimer():
        with orch_stop.claim_lock(case.store, 1):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=claimer)
    thread.start()
    held.wait(10)
    try:
        assert orch_stop.recover_requests(case.ctx, scheduler.TickReport()) == {1}
    finally:
        release.set()
        thread.join()
    assert orch_stop.read_request(case.store, 1) is not None and not _stop_events(case.store, "stop-request-expired")


def test_a_request_is_only_deleted_by_its_own_claim(case):
    old = orch_stop.write_request(case.store, case.store.get_run(1), "first", case.clock[0])
    case.clock[0] += timedelta(seconds=5)
    new = orch_stop.write_request(case.store, case.store.get_run(1), "second", case.clock[0])
    orch_stop.drop_request(case.store, 1, old)
    assert orch_stop.read_request(case.store, 1) == new
    orch_stop.drop_request(case.store, 1, new)
    assert orch_stop.read_request(case.store, 1) is None


def test_a_merge_time_from_an_earlier_run_is_not_this_runs_merge(case):
    case.store.record_merge(1, case.clock[0] - timedelta(days=1))
    run = case.store.get_run(1)
    assert not orch_stop.runner_merged(case.store, run)
    case.store.record_merge(1, case.clock[0])
    assert orch_stop.runner_merged(case.store, run)


def test_a_runner_refusal_or_tracker_write_after_a_request_is_left_to_the_stop(case, monkeypatch):
    from foragent.actions import flag
    from foragent.reasons import Reason

    case.store.update_run(1, pid=COMMAND)  # This process is the run's runner.
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    with pytest.raises(orch_stop.StopRequested):
        flag(case.ctx, 1, Reason.RETRIES_EXCEEDED, "Re-run review FAIL.")
    assert case.store.load_flag(1) is None and case.tracker.calls == []
    with orch_stop.guard_runner(case.ctx, 1):
        with pytest.raises(orch_stop.StopRequested):
            case.ctx.tracker.add_labels(1, ["blocked"])
        assert case.ctx.tracker.get_issue(1) is case.tracker.issue, "Reads still pass"
    assert case.ctx.tracker is case.tracker, "The runner's context gets its own tracker back"
    case.store.update_run(1, pid=RUNNER)  # The gate or the stop command is another process: it may flag.
    flag(case.ctx, 1, Reason.RETRIES_EXCEEDED, "Stop refused")
    assert case.store.load_flag(1) is not None


def test_a_ready_issue_with_a_stop_request_is_not_started(case, monkeypatch):
    from foragent import scheduler_start

    issue = SimpleNamespace(number=1, state="open", is_pull_request=False, labels={"approved", "ready"})
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    monkeypatch.setattr(scheduler, "_machine_budget", lambda *a: None)
    monkeypatch.setattr(scheduler, "_has_slot", lambda *a: True)
    monkeypatch.setattr(scheduler_start, "_lane_held", lambda *a: False)
    monkeypatch.setattr(scheduler_start, "_at_plan_width", lambda *a: False)
    monkeypatch.setattr(scheduler_start, "_ranked", lambda ctx, ready: [("main", 1)])
    monkeypatch.setattr(scheduler, "_local_issue", lambda ctx, n: issue)
    case.tracker.issue = issue
    case.ctx.launcher = lambda n: pytest.fail("A stop request owns the issue until it is deleted")
    scheduler_start._start_ready(case.ctx, {"main": [1]}, lambda i: "main", "origin/main",
                                 scheduler.TickReport(), sample=("normal", 0))


def test_preservation_restores_the_approved_acceptance_blobs_not_head(tmp_path):
    import subprocess

    from foragent.git import GitRepo
    from foragent.run_publish import _restore_approved_blobs

    def git(*args, input=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True, input=input).stdout.strip()

    git("init", "-q")
    approved = git("hash-object", "-w", "--stdin", input="def test_x():\n    assert True\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_x():\n    pass  # weakened\n")
    git("add", "-A")  # The executor committed the weakened locked test (plumbing: no porcelain commit).
    head = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", git("write-tree"), "-m", "weakened")
    git("update-ref", "HEAD", head)
    (tmp_path / "tests/test_new.py").write_text("def test_new():\n    pass\n")
    git("add", "-A")  # An executor added a locked file approved as absent.
    approval = SimpleNamespace(acceptance_blobs={"tests/test_x.py": approved, "tests/test_new.py": "missing"})
    _restore_approved_blobs(GitRepo(tmp_path), approval)
    assert git("rev-parse", ":tests/test_x.py") == approved
    assert git("ls-files", "--", "tests/test_new.py") == ""


def test_a_tick_decision_holds_the_claim_so_a_late_request_cannot_slip_in(case):
    with orch_stop.tick_owns(case.store, 1) as owned:
        assert owned
        with orch_stop.tick_owns(case.store, 1) as nested:  # _kill_run inside the decision keeps the claim.
            assert nested
        seen = []
        thread = threading.Thread(target=lambda: seen.append(
            orch_stop.claim_lock(case.store, 1, blocking=False).__enter__()))
        thread.start()
        thread.join()
        assert seen == [False], "A stop command cannot claim N in the middle of the tick's decision"
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    with orch_stop.tick_owns(case.store, 1) as owned:
        assert not owned, "With a request the dead-run and kill paths leave N to the stop"


def test_a_dead_runner_with_a_surviving_descendant_is_finished_as_a_stop_not_expired(case, monkeypatch):
    case.store.update_run(1, process_identities=[{"pid": RUNNER, "start_time": 5.0},
                                                 {"pid": 42003, "start_time": 9.0}])
    orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    case.table.rows.pop(COMMAND)
    case.table.rows.pop(RUNNER)
    case.table.rows[42003] = ProcessIdentity(42003, 1, 42003, 42003, 9.0)
    kills = []

    def kill(ctx, number, report, **kwargs):
        kills.append(kwargs)
        case.table.rows.pop(42003)
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch_stop.recover_requests(case.ctx, scheduler.TickReport())
    assert kills == [{"outcome": "stopped"}] and not _stop_events(case.store, "stop-request-expired")
    assert case.store.get_run(1).outcome == "stopped" and orch_stop.read_request(case.store, 1) is None


def test_processes_found_by_a_scan_are_kept_in_the_request_for_later_owners(case, monkeypatch):
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: False)  # The leftover does not die yet.
    case.table.rows[42006] = ProcessIdentity(42006, RUNNER, 42006, 42006, 8.0)
    record = orch_stop.write_request(case.store, case.store.get_run(1), "why", case.clock[0])
    assert orch_stop.scan_tree(case.store, case.store.get_run(1), record)
    assert orch_stop.read_request(case.store, 1)["seen"]["42006"] == 8.0
    case.table.rows.pop(RUNNER)
    case.table.rows[42006] = ProcessIdentity(42006, 1, 42006, 42006, 8.0)  # Reparented; the command died.
    case.table.rows.pop(COMMAND)
    assert orch_stop.recover_requests(case.ctx, scheduler.TickReport()) == {1}
    assert case.store.get_run(1).status == "running", "A live reparented descendant blocks the save"


def test_restore_puts_the_checkpointed_work_back_in_a_fresh_worktree(tmp_path):
    import subprocess

    from foragent.git import GitRepo
    from foragent.orch_stop_checkpoint import restore

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args, cwd=root, input=None):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True, input=input).stdout.strip()

    git("init", "-q")
    (root / "a.txt").write_text("base\n")
    git("add", "-A")
    base = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", git("write-tree"), "-m", "base")
    git("update-ref", "refs/heads/foragent/issue-1", base)
    (root / "a.txt").write_text("unfinished work\n")
    git("add", "-A")
    tree = git("write-tree")  # The run's staged work, as the checkpoint recorded it.
    worktree = tmp_path / "wt"
    repo = GitRepo(root)
    repo.add_detached_worktree(worktree, base)  # A half-rebuilt worktree on the base.
    ctx = SimpleNamespace(git=repo)
    restore(ctx, worktree, {"head": base, "tree": tree, "branch": "foragent/issue-1", "branch_tip": base})
    assert (worktree / "a.txt").read_text() == "unfinished work\n"
    assert git("write-tree", cwd=worktree) == tree

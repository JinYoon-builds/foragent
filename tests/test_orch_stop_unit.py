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
            case.table.rows.pop(RUNNER)
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


def test_a_busy_worktree_lock_exits_after_5_minutes_with_the_request_kept(case, monkeypatch):
    _, kill = _killer(case)
    monkeypatch.setattr(scheduler, "_kill_run", kill)
    held, release = threading.Event(), threading.Event()

    def holder():
        with case.store.worktree_lock(1):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(10)
    try:
        with pytest.raises(orch.OrchError, match="worktree-lock"):
            orch.stop(case.ctx, 1, "reprioritize")
    finally:
        release.set()
        thread.join()
    assert len(case.sleeps) == orch_stop.LOCK_WAIT_S
    assert orch_stop.read_request(case.store, 1) is not None
    assert case.store.get_run(1).status == "running", "Nothing is written before the lock is held"


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
        self.push_error, self.pushes = push_error, []

    def head_sha(self):
        return "abc123def4567890"

    def _call(self, args):
        if args[0] == "push":
            self.pushes.append(args)
            if self.push_error:
                raise GitError(self.push_error)
        return ""


def _saved_worktree(case, monkeypatch, worktree):
    path = case.ctx.repo_root / "wt"
    path.mkdir()
    removed = []
    case.ctx.git = SimpleNamespace(at=lambda root: worktree, remove_worktree=lambda root: removed.append(root))
    monkeypatch.setattr("foragent.run_publish._preserve_failed_implementation",
                        lambda n, ctx, git, reason, **kwargs: "saved" if reason == "stopped: reprioritize" else None)
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

        def remove_worktree(self, path):
            with store.worktree_lock(1):  # Main's removal takes the lock itself; it reenters here.
                seen.append(("remove", _locked_from_another_thread(store)))

        def add_worktree(self, *args):
            seen.append(("add", _locked_from_another_thread(store)))

        def _call(self, args):
            seen.append((args[0], _locked_from_another_thread(store)))
            return ""

    git = Git()
    attempt = SimpleNamespace(n=1, ctx=SimpleNamespace(store=store, git=git), git=git, worktree=case.ctx.repo_root / "wt",
                              start_ref="old", branch="foragent/issue-1", target_branch="batch-1", log=case.ctx.repo_root / "log")
    error = PatchReplayError("conflict", own_paths=[], source_head="head", source_tree="tree", source_target="main")
    run_conflicts._restore_replay_source(attempt, error)
    assert seen == [("remove", True), ("add", True), ("skills", True), ("read-tree", True)]
    assert not _locked_from_another_thread(store)

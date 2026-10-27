"""Boundary tests for `fa orch stop` beyond the locked acceptance files.

Processes are a fake table only: no test sends a real signal.
"""

from datetime import UTC, datetime
import json
import threading
from types import SimpleNamespace

import pytest

from foragent import orch, orch_stop, processes, scheduler
from foragent.processes import ProcessIdentity, ProcessTable
from foragent.reasons import Reason
from foragent.scheduler_stop import _restore_stopped
from foragent.state import StateStore

RUNNER = 42000


class _Tracker:
    def __init__(self, state="open", labels=("approved", "running"), pr=None):
        self.issue = SimpleNamespace(number=1, state=state, labels=frozenset(labels), title="Locale")
        self.pr, self.calls = pr, []

    def get_issue(self, n):
        return self.issue

    def find_pr(self, head):
        return self.pr

    def add_labels(self, n, labels):
        self.calls.append(("add", tuple(labels)))
        self.issue.labels = self.issue.labels | set(labels)

    def remove_labels(self, n, labels):
        self.calls.append(("remove", tuple(labels)))
        self.issue.labels = self.issue.labels - set(labels)

    def comment(self, n, body):
        self.calls.append(("comment", body.splitlines()[0]))


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [0.0]
    store = StateStore(tmp_path / "state", lambda: datetime(2026, 10, 27, tzinfo=UTC))
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    store.update_run(1, pid=RUNNER, process_identities=[{"pid": RUNNER, "start_time": 5.0}])
    monkeypatch.setattr(processes, "list_processes",
                        lambda: ProcessTable((ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0),)))
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *args: None)
    tracker = _Tracker()

    def sleep(seconds):
        clock[0] += seconds

    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=tracker, store=store, git=None,
                          config=SimpleNamespace(language="en"), now=lambda: datetime(2026, 10, 27, tzinfo=UTC),
                          sleep=sleep, monotonic=lambda: clock[0])
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(orch_stop, "RecordingTracker", lambda tracker, context, **kw: tracker)
    return SimpleNamespace(ctx=ctx, store=store, tracker=tracker, clock=clock)


def _events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_busy_gate_waits_60_seconds_then_refuses_without_changes(case, monkeypatch):
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("No kill without the tick lock"))
    held, release = threading.Event(), threading.Event()

    def tick():
        with case.store.hold_tick_lock():
            held.set()
            release.wait(10)

    thread = threading.Thread(target=tick)
    thread.start()
    held.wait(10)
    try:
        with pytest.raises(orch.OrchError, match="gate is busy"):
            orch.stop(case.ctx, 1, "reprioritize")
    finally:
        release.set()
        thread.join()
    assert case.clock[0] == orch_stop.TICK_LOCK_WAIT_S
    assert case.store.get_run(1).status == "running" and case.tracker.calls == [] and not _events(case.store)


@pytest.mark.parametrize("why", ["", "   \n\t"])
def test_an_empty_why_is_refused_like_a_missing_one(case, monkeypatch, why):
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("No kill without a why"))
    with pytest.raises(orch.OrchError, match="--why"):
        orch.stop(case.ctx, 1, why)


def test_a_reused_pid_with_another_start_time_is_refused_before_any_kill(case, monkeypatch):
    monkeypatch.setattr(processes, "list_processes",
                        lambda: ProcessTable((ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 99.0),)))
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("A reused PID is not ours"))
    with pytest.raises(orch.OrchError, match="different process"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert case.store.get_run(1).status == "running"


@pytest.mark.parametrize("table", [
    lambda: ProcessTable((ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0),)),  # identity unrecorded below
    lambda: (_ for _ in ()).throw(PermissionError("ps denied")),
])
def test_missing_identity_or_unreadable_table_is_left_to_the_kill_path(case, monkeypatch, table):
    case.store.update_run(1, process_identities=[])
    monkeypatch.setattr(processes, "list_processes", table)
    assert orch_stop.unsafe_target(case.store.get_run(1)) is None


def test_a_both_none_run_is_not_running(case, monkeypatch):
    case.store.update_run(1, pid=None, executor_pgid=None)
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("Nothing to kill"))
    with pytest.raises(orch.OrchError, match="not running"):
        orch.stop(case.ctx, 1, "reprioritize")


def test_the_stop_kills_through_the_stall_path_and_returns_the_issue_to_ready(case, monkeypatch):
    calls = []

    def kill(ctx, number, report, **kwargs):
        calls.append(kwargs)
        ctx.store.finish_run(number, outcome="stopped", detail=kwargs["detail"])
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    summary = orch.stop(case.ctx, 1, "  reprioritize   the locale  ")
    assert calls == [{"outcome": "stopped", "detail": "reprioritize the locale"}]
    assert "reprioritize the locale" in summary
    assert case.tracker.issue.labels == {"approved", "ready"}
    [event] = [event for event in _events(case.store) if event["kind"] == "stop"]
    assert event["issue"] == 1 and "reprioritize the locale" in event["summary"]
    assert ("comment", "<!-- foragent:orch -->") in case.tracker.calls, "The orch audit comment"


def test_a_kill_path_refusal_is_reported_with_its_own_changes(case, monkeypatch):
    def refused(ctx, number, report, **kwargs):
        report.actions.append("#1 Stop refused: not ours")
        return False

    monkeypatch.setattr(scheduler, "_kill_run", refused)
    with pytest.raises(orch.OrchError, match="Stop refused: not ours"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert not [event for event in _events(case.store) if event["kind"] == "stop"]


def test_a_run_finished_before_the_kill_is_not_running_and_unchanged(case, monkeypatch):
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: False)  # The runner merged first.
    with pytest.raises(orch.OrchError, match="not running"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert case.tracker.calls == [] and not _events(case.store)


def _restore(case, **tracker):
    case.ctx.tracker = _Tracker(**tracker)
    flags = []
    scheduler_flag = scheduler.flag
    try:
        scheduler.flag = lambda ctx, n, reason, summary, *rest: flags.append(reason)
        report = scheduler.TickReport()
        _restore_stopped(case.ctx, 1, "reprioritize", report)
    finally:
        scheduler.flag = scheduler_flag
    return case.ctx.tracker, flags


def test_an_open_pr_is_flagged_as_a_stall_and_kept_open(case):
    tracker, flags = _restore(case, pr=17)
    assert flags == [Reason.STALLED] and not any(call[0] == "add" for call in tracker.calls)
    assert len([event for event in _events(case.store) if event["kind"] == "stop"]) == 1


@pytest.mark.parametrize("tracker", [{"state": "closed"}, {"labels": ("approved", "running", "blocked")},
                                     {"labels": ("approved", "running", "needs-human")}])
def test_a_closed_blocked_or_needs_human_issue_only_loses_running(case, tracker):
    result, flags = _restore(case, **tracker)
    assert result.calls == [("remove", ("running",))] and flags == []
    assert len([event for event in _events(case.store) if event["kind"] == "stop"]) == 1


def test_an_orphaned_stopped_run_is_restored_by_the_next_tick(case, monkeypatch):
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    seen = []
    monkeypatch.setattr("foragent.scheduler_recovery._restore_stopped", lambda ctx, n, why, report: seen.append((n, why)))
    monkeypatch.setattr(scheduler, "_remove_run_worktree", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_local_issue", lambda ctx, n: None)
    issue = SimpleNamespace(number=1, labels=frozenset({"running"}), state="open")
    scheduler._recover_orphan(case.ctx, issue, scheduler.TickReport())
    assert seen == [(1, "reprioritize")], "A stop command that died after its kill is finished by the tick"


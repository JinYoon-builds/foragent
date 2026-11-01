"""Boundary tests for `fa orch stop` beyond the locked acceptance files.

Processes are a fake table only: no test sends a real signal.
"""

from datetime import UTC, datetime
import json
import threading
from types import SimpleNamespace

import pytest

from foragent import orch, orch_stop, processes, scheduler
from foragent.config import Config
from foragent.processes import ProcessIdentity, ProcessTable
from foragent.reasons import ORCH_MARKER, Reason, flag_text, parse_reason
from foragent.scheduler_stop import _restore_stopped
from foragent.state import StateStore
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError

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
        self.calls.append(("comment", body.partition("-->")[0] + "-->"))  # The marker of a one-line comment.


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
    # The record is unfinished, so status lists it (#1207); #261 still refuses to signal the PID.
    with pytest.raises(orch.OrchError, match="a different process now has this PID"):
        orch.stop(case.ctx, 1, "reprioritize")
    assert case.store.get_run(1).status == "running"


@pytest.mark.parametrize("outcome", ["stopped", "killed"])
def test_an_explicit_stop_of_gone_workers_keeps_the_work_while_the_tick_records_a_crash(
    case, monkeypatch, outcome,
):
    """#1207: a "gone" result finishes an explicit stop as stopped; the tick's kill stays a crash."""
    from foragent import scheduler_recovery

    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes", lambda *a, **k: "gone")
    saved, dead = [], []
    monkeypatch.setattr(scheduler, "_remove_run_worktree",
                        lambda ctx, n, wt, report, *, outcome, why="": saved.append((n, outcome, why)))
    monkeypatch.setattr(scheduler_recovery, "_finish_dead_run", lambda ctx, run, report: dead.append(run.number))
    ctx = SimpleNamespace(store=case.store, now=case.ctx.now)
    detail = "keep it" if outcome == "stopped" else ""
    killed = scheduler_recovery._kill_run(ctx, 1, scheduler.TickReport(), outcome=outcome, detail=detail)
    if outcome == "stopped":
        assert killed and saved == [(1, "stopped", "keep it")] and dead == []
        assert (case.store.get_run(1).status, case.store.get_run(1).outcome) == ("finished", "stopped")
        assert [run.outcome for run in case.store.read_history()] == ["stopped"]
    else:
        assert not killed and saved == [] and dead == [1] and case.store.get_run(1).status == "running"


def test_stop_reads_the_same_unfinished_records_as_status_active_runs(case, monkeypatch):
    """#1207: an implementing record whose workers are gone is listed by status and stopped."""
    from foragent import status

    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable(()))  # The workers are gone.
    case.store.update_run(1, phase="implementing")
    listed = status.collect(case.store, Config(), datetime(2026, 10, 27, tzinfo=UTC))["active_runs"]
    assert [(run["number"], run["status"], run["phase"]) for run in listed] == [(1, "running", "implementing")]
    calls = []

    def kill(ctx, number, report, **kwargs):
        calls.append(number)
        ctx.store.finish_run(number, outcome="stopped", detail=kwargs["detail"])
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(case.ctx, 1, "reprioritize")
    assert calls == [1] and case.store.get_run(1).status == "finished"
    assert status.collect(case.store, Config(), datetime(2026, 10, 27, tzinfo=UTC))["active_runs"] == []


@pytest.mark.parametrize("table", [
    lambda: ProcessTable((ProcessIdentity(RUNNER, 1, RUNNER, RUNNER, 5.0),)),  # identity unrecorded below
    lambda: (_ for _ in ()).throw(PermissionError("ps denied")),
])
def test_missing_identity_or_unreadable_table_is_left_to_the_kill_path(case, monkeypatch, table):
    case.store.update_run(1, process_identities=[])
    monkeypatch.setattr(processes, "list_processes", table)
    assert orch_stop.unsafe_target(case.store.get_run(1)) is None


def test_a_both_none_run_is_running_and_stopped_through_the_kill_path(case, monkeypatch):
    """#435: park and the status row call a record without processes running, so stop does too."""
    from foragent.scheduler_stop import run_is_live

    case.store.update_run(1, pid=None, executor_pgid=None, process_identities=[])
    assert run_is_live(case.store.get_run(1))
    calls = []

    def kill(ctx, number, report, **kwargs):
        calls.append(number)
        ctx.store.finish_run(number, outcome="stopped", detail=kwargs["detail"])
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(case.ctx, 1, "reprioritize")
    assert calls == [1] and case.store.get_run(1).status != "running"


def test_a_finished_run_is_not_running(case, monkeypatch):
    case.store.finish_run(1, outcome="error")
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


def _recording_tracker(case):
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    tracker = FakeTracker(now=case.ctx.now)
    tracker.add_issue(1, "Locale", labels=["approved", "running"])
    tracker.create_pr(head="foragent/issue-1", base="main", title="Locale", body="WIP")
    case.ctx.config = Config()
    case.ctx.tracker = RecordingTracker(tracker, case.ctx, queue_writes=True)
    case.ctx.tracker.get_issue(1)
    return tracker


@pytest.mark.parametrize("limited_action", [None, "remove_labels", "comment"])
def test_an_open_pr_is_flagged_as_a_stall_and_kept_open(case, monkeypatch, limited_action):
    tracker = _recording_tracker(case)
    if limited_action:
        original = getattr(tracker, limited_action)
        limited = [True]

        def write(*args):
            if limited:
                limited.pop()
                raise TrackerError("API rate limit exceeded")
            return original(*args)

        monkeypatch.setattr(tracker, limited_action, write)
    why = "reprioritize ↵ locale"
    _restore_stopped(case.ctx, 1, why, scheduler.TickReport())
    _restore_stopped(case.ctx, 1, why, scheduler.TickReport())
    assert case.ctx.tracker.current_issue(1).labels == {"approved", "blocked"}
    assert len(_stops(case)) == 1
    case.ctx.tracker.flush_outbox()
    bodies = [comment.body for comment in tracker.list_comments(1)]
    assert len(bodies) == 1 and len(bodies[0].splitlines()) == 1, "One stop owes one public line, including after recovery"
    assert parse_reason(bodies[0]) == Reason.STALLED
    summary, evidence = flag_text(bodies[0])
    assert summary == case.store.load_flag(1).summary == "Runner stopped with an open PR; rerun review or retry implementation."
    assert evidence.startswith(ORCH_MARKER) and why in evidence, "The same line keeps the exact stop audit"
    assert tracker.get_issue(1).labels == {"approved", "blocked"}
    assert tracker.find_pr("foragent/issue-1") == 2 and tracker.deleted_branches == []


@pytest.mark.parametrize("tracker", [{"state": "closed"}, {"labels": ("approved", "running", "blocked")},
                                     {"labels": ("approved", "running", "needs-human")}])
def test_a_closed_blocked_or_needs_human_issue_only_loses_running(case, tracker):
    result, flags = _restore(case, **tracker)
    assert result.calls == [("comment", "<!-- foragent:orch -->"), ("remove", ("running",))] and flags == []
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


def _stops(case):
    return [event for event in _events(case.store) if event["kind"] == "stop"]


def test_a_board_rate_limit_after_ready_keeps_running_for_the_retry_and_records_once(case, monkeypatch):
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    limited = [TrackerError("API rate limit exceeded")]

    def board(ctx, n, status):
        if limited:
            raise limited.pop()

    monkeypatch.setattr(scheduler.board, "sync_status", board)
    case.ctx.tracker = _Tracker()
    with pytest.raises(TrackerError):
        _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert len(_stops(case)) == 1, "The stop event is recorded before the step that failed"
    assert ("comment", "<!-- foragent:orch -->") in case.ctx.tracker.calls, "So is the audit comment"
    assert "running" in case.ctx.tracker.issue.labels, "Orphan recovery still finds the issue"
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())  # The next tick's retry.
    assert len(_stops(case)) == 1
    assert case.ctx.tracker.calls.count(("comment", "<!-- foragent:orch -->")) == 1, (
        "Completing the same stop after a board failure must not announce it twice")
    assert case.ctx.tracker.issue.labels == {"approved", "ready"}
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert case.ctx.tracker.calls.count(("comment", "<!-- foragent:orch -->")) == 1, "Ready already: no transition"
    assert len(_stops(case)) == 1


@pytest.mark.parametrize("failed_step", ["board", "flag", "remove_labels"])
def test_a_failing_open_pr_flag_still_leaves_the_stop_event_and_comment(case, monkeypatch, failed_step):
    tracker = _recording_tracker(case)
    owner, method = ((scheduler.board, "sync_status") if failed_step == "board" else
                     (scheduler, "flag") if failed_step == "flag" else (tracker, "remove_labels"))
    original, limited = getattr(owner, method), [True]

    def fail_once(*args, **kwargs):
        if limited:
            limited.pop()
            raise RuntimeError("flag transition unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, fail_once)
    with pytest.raises(RuntimeError, match="flag transition unavailable"):
        _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    first = tracker.list_comments(1)
    assert len(_stops(case)) == len(first) == 1 and ORCH_MARKER in first[0].body
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert len(_stops(case)) == 1 and tracker.list_comments(1) == first
    assert case.ctx.tracker.current_issue(1).labels == {"approved", "blocked"}
    case.ctx.tracker.flush_outbox()
    assert tracker.get_issue(1).labels == {"approved", "blocked"}
    assert len(_stops(case)) == 1 and tracker.list_comments(1) == first


@pytest.mark.parametrize("state,labels", [
    ("closed", ("approved", "running")),
    ("open", ("blocked", "running")), ("open", ("needs-human", "running")),
])
def test_stop_audit_is_not_repeated_when_label_removal_fails(case, monkeypatch, state, labels):
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    tracker = case.ctx.tracker = _Tracker(state=state, labels=labels)
    remove, limited = tracker.remove_labels, [True]

    def remove_once(*args):
        if limited:
            limited.pop()
            raise RuntimeError("label removal unavailable")
        remove(*args)

    monkeypatch.setattr(tracker, "remove_labels", remove_once)
    with pytest.raises(RuntimeError, match="label removal unavailable"):
        _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert "running" in tracker.issue.labels
    assert len(_stops(case)) == tracker.calls.count(("comment", ORCH_MARKER)) == 1
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert tracker.issue.labels == set(labels) - {"running"}
    assert len(_stops(case)) == tracker.calls.count(("comment", ORCH_MARKER)) == 1


def test_the_command_posts_one_audit_comment(case, monkeypatch):
    def kill(ctx, number, report, **kwargs):
        ctx.store.finish_run(number, outcome="stopped", detail=kwargs["detail"])
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(case.ctx, 1, "reprioritize")
    assert case.tracker.calls.count(("comment", "<!-- foragent:orch -->")) == 1


@pytest.mark.parametrize("state,labels", [
    ("closed", ("approved",)), ("open", ("blocked",)),
    ("open", ("needs-human",)), ("open", ("approved", "ready")),
])
def test_stop_restoration_comments_only_when_the_observed_issue_state_changes(case, monkeypatch, state, labels):
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    case.ctx.tracker = _Tracker(state=state, labels=labels)
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert not [call for call in case.ctx.tracker.calls if call[0] == "comment"]
    assert len(_stops(case)) == 1, "Every completed stop still has its run journal event"
    case.ctx.tracker.issue.labels |= {"running"}
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert not [call for call in case.ctx.tracker.calls if call[0] == "comment"], "A recovery retry belongs to the same stop"
    now = lambda: datetime(2026, 10, 28, tzinfo=UTC)
    monkeypatch.setattr(case.store, "_now", now)
    monkeypatch.setattr(case.ctx, "now", now)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    case.store.finish_run(1, outcome="stopped", detail="reprioritize")
    case.ctx.tracker.issue.labels |= {"running"}
    _restore_stopped(case.ctx, 1, "reprioritize", scheduler.TickReport())
    assert case.ctx.tracker.calls.count(("comment", "<!-- foragent:orch -->")) == 1
    assert "running" not in case.ctx.tracker.issue.labels



def test_stop_keeps_the_opening_snapshot_when_the_kill_changes_labels(case, monkeypatch):
    # An exited runner can have removed running by the time restoration begins.
    case.tracker.issue.labels = frozenset({"running", "approved"})

    def kill(ctx, number, report, **kwargs):
        case.tracker.issue.labels = frozenset({"blocked", "approved"})
        ctx.store.finish_run(number, outcome="stopped", detail=kwargs["detail"])
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(case.ctx, 1, "reprioritize")
    assert case.tracker.calls.count(("comment", "<!-- foragent:orch -->")) == 1
    assert case.tracker.issue.labels == {"blocked", "approved"}

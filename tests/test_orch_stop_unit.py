"""Boundary tests for `fa orch stop` beyond the locked acceptance file."""

from datetime import UTC, datetime
import threading
from types import SimpleNamespace

import pytest

from foragent import orch, processes, scheduler
from foragent.processes import ProcessIdentity, ProcessTable
from foragent.run_publish import _preserve_failed_implementation
from foragent.scheduler_stop import stop_target_refusal
from foragent.state import Run, StateStore


def _run(**fields):
    base = dict(number=1, branch="foragent/issue-1", worktree="/nowhere", pid=42000,
                started_at="2026-10-26T00:00:00+00:00", last_activity_at="2026-10-26T00:00:00+00:00",
                last_progress_at="2026-10-26T00:00:00+00:00",
                status="running", executor_pgid=None,
                process_identities=[{"pid": 42000, "start_time": 5.0}])
    return Run(**{**base, **fields})


@pytest.mark.parametrize("field", ["pid", "executor_pgid"])
def test_unreadable_recorded_pid_is_refused(monkeypatch, field):
    # Unreadable rows cannot prove ownership; a missing row would wrongly pass.
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable((), unreadable={42000}))
    refusal = stop_target_refusal(_run(**{"pid": None, field: 42000}))
    assert refusal is not None and "cannot be read" in refusal


@pytest.mark.parametrize(("pid", "identities"), [
    (None, []),  # launched but never recorded: nothing proves which process to stop
    (42000, []),  # a numeric PID without its recorded birth is not ownership
    (42000, [{"pid": 41999, "start_time": 5.0}]),
])
def test_runner_without_a_recorded_identity_is_refused(monkeypatch, pid, identities):
    # The PID is not even listed: absence of a live row must not count as safe.
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable(()))
    refusal = stop_target_refusal(_run(pid=pid, process_identities=identities))
    assert refusal is not None and "ownership cannot be verified" in refusal


def test_recorded_live_owned_pid_passes(monkeypatch):
    row = ProcessIdentity(42000, 1, 42000, 42000, 5.0)
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable((row,)))
    assert stop_target_refusal(_run()) is None


class _Git:
    def __init__(self):
        self.local_skill_paths, self.commits, self.pushed = set(), [], []

    def conflict_paths(self):
        return []

    def merge_in_progress(self):
        return False

    def stage_changes(self):
        pass

    def has_staged_changes(self):
        return True

    def commit(self, message):
        self.commits.append(message)

    def head_sha(self):
        return "wip"

    def push(self, branch):
        self.pushed.append(branch)


@pytest.mark.parametrize(("outcome", "detail", "reason"), [
    ("stopped", "stopped: keep the locale work", "stopped: keep the locale work"),
    ("killed", "Runner stopped by the scheduler.", "killed"),
])
def test_wip_names_the_recorded_stop_reason_whoever_saves_it(outcome, detail, reason):
    run = _run(outcome=outcome, detail=detail)
    store = SimpleNamespace(get_run=lambda n: run, log_path=lambda n: None)
    ctx = SimpleNamespace(store=store, tracker=SimpleNamespace(find_pr=lambda branch: None))
    git = _Git()
    _preserve_failed_implementation(1, ctx, git, run.outcome, approval=None, title="Locale")
    assert git.commits == [f"WIP: Locale (#1) [{reason}]"]


def test_refused_kill_restores_the_recorded_outcome(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", lambda: datetime(2026, 10, 26, tzinfo=UTC))
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    store.update_run(1, detail="Implementing.")
    monkeypatch.setattr("foragent.scheduler_stop.stop_target_refusal", lambda run: None)
    seen = []

    def refused(ctx, number, report, **kwargs):
        seen.append(ctx.store.get_run(number).outcome)
        report.actions.append("#1 Stop refused: not ours")
        return False

    monkeypatch.setattr(scheduler, "_kill_run", refused)
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=None, store=store, config=None,
                          git=None, now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**kw))
    with pytest.raises(orch.OrchError, match="stop did not complete"):
        orch.stop(ctx, 1, "reprioritize")
    run = store.get_run(1)
    assert seen == ["stopped"], "The stop intent must be recorded before any signal"
    assert (run.status, run.outcome, run.detail) == ("running", None, "Implementing.")


class _Labels:
    def __init__(self):
        self.calls, self.pr = [], None
        self.issue = SimpleNamespace(state="open", labels={"approved", "running"})

    def find_pr(self, head):
        return self.pr

    def get_issue(self, n):
        return self.issue

    def add_labels(self, n, labels):
        self.calls.append(("add", n, tuple(labels)))

    def remove_labels(self, n, labels):
        self.calls.append(("remove", n, tuple(labels)))


def _running_store(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", lambda: datetime(2026, 10, 26, tzinfo=UTC))
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    monkeypatch.setattr("foragent.scheduler_stop.stop_target_refusal", lambda run: None)
    labels = _Labels()
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**{**kw, "tracker": labels}))
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *args: None)
    events = []
    monkeypatch.setattr(orch, "_event", lambda ctx, n, kind, summary: events.append((n, kind, summary)))
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=labels, store=store, config=None,
                          git=None, now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    return store, ctx, labels, events


def test_stop_holds_the_tick_lock_until_labels_are_ready(tmp_path, monkeypatch, kicks):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)
    tick_ran = []

    def kill(tick_ctx, number, report, **kwargs):
        # A runner's kicked tick runs in another process; another thread models it.
        thread = threading.Thread(target=lambda: tick_ran.append(_try_tick(store)))
        thread.start()
        thread.join()
        store.finish_run(number, outcome="stopped")
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    orch.stop(ctx, 1, "reprioritize")
    assert tick_ran == [False], "A tick could recover the stopped run before its labels were set"
    assert labels.calls == [("add", 1, ("ready",)), ("remove", 1, ("running",))]
    assert [kind for _, kind, _ in events] == ["stop"] and kicks == [tmp_path]
    assert _try_tick(store), "The tick lock must be released after the stop"


def _try_tick(store):
    with store.tick_lock() as acquired:
        return acquired


def test_busy_tick_lock_refuses_without_changes(tmp_path, monkeypatch):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "STOP_LOCK_WAIT_S", 0)
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **kw: pytest.fail("Stop ran during a tick"))
    before = store.get_run(1)
    with store._try_tick_lock(store._tick_lock_path()) as held:
        assert held
        with pytest.raises(orch.OrchError, match="tick held the lock"):
            orch.stop(ctx, 1, "reprioritize")
    assert store.get_run(1) == before and labels.calls == [] and events == []


@pytest.mark.parametrize("outcome", ["killed", "wip-not-saved"])
def test_kill_success_with_another_finished_outcome_is_not_a_stop(tmp_path, monkeypatch, outcome):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)

    def kill(tick_ctx, number, report, **kwargs):
        # A runner that ignored the stop, or could not save its work, finished the record.
        store.finish_run(number, outcome=outcome)
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match=f"finished as {outcome}"):
        orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [] and events == []


def test_runner_mark_of_unsaved_work_survives_a_refused_stop(tmp_path, monkeypatch):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)

    def kill(tick_ctx, number, report, **kwargs):
        store.update_run(number, outcome="wip-not-saved")  # The runner's failed save, mid-stop.
        return False

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="still running"):
        orch.stop(ctx, 1, "reprioritize")
    assert store.get_run(1).outcome == "wip-not-saved" and labels.calls == []


def test_stop_with_an_open_pr_blocks_instead_of_a_ready_that_a_tick_would_clear(tmp_path, monkeypatch, kicks):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)
    labels.pr = 7
    flags = []
    monkeypatch.setattr(scheduler, "flag", lambda tick_ctx, n, reason, summary, details=None:
                        flags.append((n, reason, summary, details)))

    def kill(tick_ctx, number, report, **kwargs):
        store.finish_run(number, outcome="stopped")
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    summary = orch.stop(ctx, 1, "reprioritize")
    assert flags == [(1, scheduler.Reason.RETRIES_EXCEEDED,
                      "Stopped with open PR #7; rerun review or retry implementation.", "Stop reason: reprioritize")]
    assert ("add", 1, ("ready",)) not in labels.calls
    assert "PR #7 is open" in summary and "fa orch retry 1" in summary
    assert [kind for _, kind, _ in events] == ["stop"]


@pytest.mark.parametrize(("state", "labels", "held"), [
    ("open", {"approved", "running", "blocked"}, "blocked"),
    ("open", {"approved", "running", "needs-human"}, "needs-human"),
    ("closed", {"running"}, "closed"),
])
def test_stop_never_adds_ready_that_contradicts_a_hold_or_closed_issue(
    tmp_path, monkeypatch, kicks, state, labels, held,
):
    store, ctx, tracker, events = _running_store(tmp_path, monkeypatch)
    tracker.issue = SimpleNamespace(state=state, labels=labels)
    tracker.pr = 7  # The hold wins over the open-PR rule too: no second flag.
    monkeypatch.setattr(scheduler, "flag", lambda *args, **kwargs: pytest.fail("Stop re-flagged a held issue"))

    def kill(tick_ctx, number, report, **kwargs):
        store.finish_run(number, outcome="stopped")
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    summary = orch.stop(ctx, 1, "reprioritize")
    assert tracker.calls == [("remove", 1, ("running",))]
    assert f"not returned to ready because the issue is {held}" in summary
    assert [kind for _, kind, _ in events] == ["stop"]

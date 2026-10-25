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
        self.calls = []

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


def test_kill_success_with_another_finished_outcome_is_not_a_stop(tmp_path, monkeypatch):
    store, ctx, labels, events = _running_store(tmp_path, monkeypatch)

    def kill(tick_ctx, number, report, **kwargs):
        # A runner whose handler ignored the stop finishes the record first.
        store.finish_run(number, outcome="killed")
        return True

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="finished as killed"):
        orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [] and events == []

"""Boundary tests for `fa orch stop` beyond the locked acceptance file."""

from datetime import UTC, datetime
import json
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


@pytest.mark.parametrize(("outcome", "detail", "stop_why", "reason"), [
    ("stopped", "stopped: keep the locale work", "keep the locale work", "stopped: keep the locale work"),
    # The dying runner overwrote outcome and detail; the dedicated field still names the why.
    ("error", "Runner failed: executor killed", "keep the locale work", "stopped: keep the locale work"),
    ("killed", "Runner stopped by the scheduler.", "", "killed"),
])
def test_wip_names_the_recorded_stop_reason_whoever_saves_it(tmp_path, outcome, detail, stop_why, reason):
    run = _run(outcome=outcome, detail=detail, stop_why=stop_why)
    store = SimpleNamespace(get_run=lambda n: run, log_path=lambda n: None, root=tmp_path,
                            update_run=lambda n, **fields: None)
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

    def close_pr(self, pr, comment=None):
        self.calls.append(("close", pr))

    def comment(self, n, body):
        self.calls.append(("comment", n))

    def add_labels(self, n, labels):
        self.calls.append(("add", n, tuple(labels)))

    def remove_labels(self, n, labels):
        self.calls.append(("remove", n, tuple(labels)))


READY = [("add", 1, ("ready",)), ("remove", 1, ("running", "blocked", "needs-human"))]


def _running_store(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", lambda: datetime(2026, 10, 26, tzinfo=UTC))
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    monkeypatch.setattr("foragent.scheduler_stop.stop_target_refusal", lambda run: None)
    labels = _Labels()
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**{**kw, "tracker": labels}))
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *args: None)
    monkeypatch.setattr(orch, "RecordingTracker", lambda tracker, context, **kwargs: tracker)
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=labels, store=store, config=None,
                          git=None, now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    return store, ctx, labels


def _journal(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _finish(store, outcome="stopped", **fields):
    def kill(tick_ctx, number, report, **kwargs):
        if fields:
            store.update_run(number, **fields)
        store.finish_run(number, outcome=outcome)
        return True
    return kill


def test_stop_holds_the_tick_lock_until_labels_are_ready(tmp_path, monkeypatch, kicks):
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
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
    assert labels.calls == [*READY, ("comment", 1)]
    assert [event["kind"] for event in _journal(store)] == ["stop"] and kicks == [tmp_path]
    assert _try_tick(store), "The tick lock must be released after the stop"


def _try_tick(store):
    with store.tick_lock() as acquired:
        return acquired


def test_busy_tick_lock_refuses_without_changes(tmp_path, monkeypatch):
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "STOP_LOCK_WAIT_S", 0)
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **kw: pytest.fail("Stop ran during a tick"))
    before = store.get_run(1)
    with store._try_tick_lock(store._tick_lock_path()) as held:
        assert held
        with pytest.raises(orch.OrchError, match="tick held the lock"):
            orch.stop(ctx, 1, "reprioritize")
    assert store.get_run(1) == before and labels.calls == [] and _journal(store) == []


@pytest.mark.parametrize(("outcome", "fields", "message"), [
    ("killed", {}, "finished as killed"),
    ("wip-not-saved", {}, "finished as wip-not-saved"),
    # The runner saved WIP for its own failure before the stop recorded its intent.
    ("stopped", {"wip_reason": "retries-exceeded"}, "already saved as WIP for retries-exceeded"),
])
def test_a_run_that_did_not_end_with_its_work_kept_for_the_why_is_not_a_stop(tmp_path, monkeypatch, outcome,
                                                                             fields, message):
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store, outcome, **fields))
    with pytest.raises(orch.OrchError, match=message):
        orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [] and _journal(store) == []


def test_runner_mark_of_unsaved_work_survives_a_refused_stop(tmp_path, monkeypatch):
    store, ctx, labels = _running_store(tmp_path, monkeypatch)

    def kill(tick_ctx, number, report, **kwargs):
        store.update_run(number, outcome="wip-not-saved")  # The runner's failed save, mid-stop.
        return False

    monkeypatch.setattr(scheduler, "_kill_run", kill)
    with pytest.raises(orch.OrchError, match="still running"):
        orch.stop(ctx, 1, "reprioritize")
    run = store.get_run(1)
    assert (run.outcome, run.stop_why) == ("wip-not-saved", "") and labels.calls == []


def test_stop_with_an_open_pr_closes_it_and_returns_ready(tmp_path, monkeypatch, kicks):
    """Finding class: alternate destinations bypass the goal; ready/resume is the only success."""
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    labels.pr = 7
    monkeypatch.setattr(scheduler, "flag", lambda *args, **kwargs: pytest.fail("Stop blocked instead of resuming"))
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store))
    summary = orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [("close", 7), *READY, ("comment", 1)]
    assert "closed PR #7" in summary and "why: reprioritize" in summary


@pytest.mark.parametrize("hold", ["blocked", "needs-human"])
def test_stop_returns_a_held_issue_to_ready_like_retry(tmp_path, monkeypatch, kicks, hold):
    """A dying runner's flag, even one that read the run before the intent, cannot hold the issue."""
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    labels.issue = SimpleNamespace(state="open", labels={"approved", "running", hold})
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store))
    orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [*READY, ("comment", 1)]


def test_a_closed_issue_is_stopped_without_ready(tmp_path, monkeypatch, kicks):
    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    labels.issue = SimpleNamespace(state="closed", labels={"running"})
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store))
    summary = orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [("remove", 1, ("running",)), ("comment", 1)]
    assert "closed, so it was not returned to ready" in summary


def test_tracker_failure_after_termination_is_pending_and_the_retry_records_one_event(tmp_path, monkeypatch, kicks):
    """Finding class: a partially completed stop stays recoverable without duplicate events."""
    from foragent.scheduler_stop import complete_stop

    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    issue = labels.issue

    def unavailable(n):
        raise RuntimeError("GitHub unavailable")

    labels.get_issue = unavailable
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store))
    with pytest.raises(orch.StopPending, match="next gate tick completes the stop"):
        orch.stop(ctx, 1, "reprioritize")
    assert [(event["kind"], "reprioritize" in event["summary"]) for event in _journal(store)] == [("stop", True)]
    assert labels.calls == [] and store.get_run(1).outcome == "stopped" and kicks == [tmp_path]
    labels.get_issue = lambda n: issue  # The tick's orphan recovery repeats the completion.
    complete_stop(SimpleNamespace(store=store, tracker=labels, now=ctx.now), store.get_run(1))
    assert [event["kind"] for event in _journal(store)] == ["stop"] and labels.calls == READY


def test_a_failed_stop_event_is_never_reported_as_success(tmp_path, monkeypatch, kicks):
    """Finding class: required persistence is not optional; labels wait until it is written."""
    store, ctx, labels = _running_store(tmp_path, monkeypatch)

    def no_journal(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler, "append_event", no_journal)
    monkeypatch.setattr(scheduler, "_kill_run", _finish(store))
    with pytest.raises(orch.StopPending, match="disk full"):
        orch.stop(ctx, 1, "reprioritize")
    assert labels.calls == [], "running must stay so the tick's orphan recovery retries the event"


class _StagingGit(_Git):
    def __init__(self, during_staging):
        super().__init__()
        self.during_staging = during_staging

    def stage_changes(self):
        self.during_staging()


@pytest.mark.parametrize("stop_during_staging", [True, False])
def test_wip_reason_reads_the_stop_intent_at_commit_time(tmp_path, monkeypatch, stop_during_staging):
    """Finding class: stale stop-intent reads; the WIP and the final outcome agree."""
    from foragent.run_publish import final_outcome, stop_kept_work

    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    record = lambda: store.update_run(1, outcome="stopped", stop_why="reprioritize")  # noqa: E731
    git = _StagingGit(record if stop_during_staging else lambda: None)
    _preserve_failed_implementation(1, ctx, git, "retries-exceeded", approval=None, title="Locale")
    if not stop_during_staging:
        record()  # The stop recorded its intent only after this WIP was committed.
    run = store.get_run(1)
    reason = "stopped: reprioritize" if stop_during_staging else "retries-exceeded"
    assert git.commits == [f"WIP: Locale (#1) [{reason}]"] and run.wip_reason == reason
    assert stop_kept_work(run) is stop_during_staging
    assert final_outcome(run, "retries-exceeded") == ("stopped" if stop_during_staging else "retries-exceeded")


def test_tick_drops_a_stop_intent_abandoned_before_any_signal(tmp_path, monkeypatch):
    """A stop process that died after recording its intent."""
    from foragent.scheduler_stop import clear_stale_stop

    store, ctx, labels = _running_store(tmp_path, monkeypatch)
    run = store.update_run(1, outcome="stopped", detail="stopped: reprioritize", stop_why="reprioritize")
    cleared = clear_stale_stop(ctx, run)
    assert (cleared.status, cleared.outcome, cleared.stop_why) == ("running", None, "")
    assert [event["kind"] for event in _journal(store)] == ["stop-abandoned"]
    assert clear_stale_stop(ctx, cleared) is cleared  # A run without an intent is untouched.

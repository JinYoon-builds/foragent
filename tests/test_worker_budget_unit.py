"""A direct rerun's own slot covers its first reviewer, as an admitted run's record does (#993)."""

from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import capacity, capacity_sessions, review_perspectives, runner
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_review_perspectives import perspective_case
from test_board_status_groups import offline_github  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def _assert_current_run(case):
    run = case.store.get_run(1)
    assert run.status == "running" and run.pid == os.getpid(), run


@pytest.mark.parametrize("entry", ["finished-issue", "running-issue", "review"])
def test_recorded_runner_birth_is_available_before_execution(
        git_history, tmp_path, monkeypatch, fake_process_table, entry):
    from foragent.scheduler_stop import runner_alive

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    table = fake_process_table
    root = table.add(table.self_pid, cwd=case.root, start_time=23)
    if entry != "running-issue":
        case.store.finish_run(1, "error")
    if entry == "review":
        case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.0))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    observed = []

    def execute(*args):
        # Read the saved record at the worker boundary, before any executor can update it.
        run = case.store.get_run(1)
        observed.append(run)
        assert run.status == "running" and run.pid == root.pid
        assert run.process_identities == [{"pid": root.pid, "start_time": root.start_time}]
        assert runner_alive(run) is True
        table.add(root.pid, cwd=case.root, start_time=root.start_time + 1)
        assert runner_alive(run) is False, "a reused PID must not stand in for the recorded runner"
        return "merged"

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_review_only", execute)
    outcome = runner.review_only(1, case.ctx) if entry == "review" else runner.run_issue(1, case.ctx)
    assert outcome == "merged" and len(observed) == 1
    assert not table.signals


@pytest.mark.parametrize("budget", [1, 2])
def test_direct_rerun_panel_uses_n_slots_and_merges_once(git_history, tmp_path, monkeypatch, budget):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=3, review_max=0, max_retries=1)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    capacity.write_state(case.store, budget, case.clock[0])
    case.store.finish_run(1, "error")  # A person reruns a finished issue: no admitted run holds a slot.
    # One whole-change reviewer per perspective (#1167); the reviewer count only caps concurrency.
    panel = len(review_perspectives.shares(case.config))
    lock, pair = Lock(), Barrier(2, timeout=3)
    active, maximum, used, reviews, merges = [0], [0], [], [], []

    def review(**kw):
        with lock:
            if len(reviews) >= panel:
                merges.append(kw)
                used.append(capacity.machine_runs(case.store, case.clock[0]))
                return ExecResult(0, False, "VERDICT: PASS", kw["log_path"])
            index = len(reviews)
            reviews.append(kw)
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            used.append(capacity.machine_runs(case.store, case.clock[0]))
        try:
            if budget == 2 and index < 2:
                pair.wait()  # Budget 2 lets the first two reviewers run together.
            return ExecResult(0, False, "VERDICT: PASS", kw["log_path"])
        finally:
            with lock:
                active[0] -= 1

    def execute(**kw):
        _assert_current_run(case)
        return case.implementer.run(**kw) if not case.implementer.calls else review(**kw)

    case.ctx.executor_factory = lambda _: SimpleNamespace(run=execute)
    monkeypatch.setattr(capacity, "WORKLOAD_POLL_SECONDS", 0.05)
    hold = capacity.hold_workload
    # A reviewer that can never be admitted fails within seconds instead of the default 600.
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **kw: hold(*a, **{**kw, "wait_s": 5}))
    assert runner.run_issue(1, case.ctx) == "merged"
    assert panel >= 3 and len(reviews) == panel and len(merges) == 1
    assert maximum[0] <= budget and max(used) <= budget, "the runner and its reviewers took N+1 slots"
    assert capacity.machine_runs(case.store, case.clock[0]) == 0


@pytest.mark.parametrize("entry", ["issue", "review"])
def test_admission_waits_as_long_as_the_run_may_take(git_history, tmp_path, monkeypatch, entry):
    """A reviewer queued behind its siblings, or a direct start behind valid work, never gives up
    at the 600 s default while the run still has time (review finding, #993)."""
    case = perspective_case(git_history, tmp_path, monkeypatch, count=1, review_max=0, max_retries=1)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    case.store.finish_run(1, "error")
    if entry == "review":
        case.tracker.remove_labels(1, ["running"])
    hold, waits = capacity.hold_workload, []

    def recorded(*args, **kw):
        waits.append((kw.get("kind"), kw.get("wait_s")))
        return hold(*args, **kw)

    monkeypatch.setattr(capacity, "hold_workload", recorded)
    runtime = case.config.max_runtime_minutes * 60
    assert runtime > 600
    if entry == "review":
        def review(*args):
            _assert_current_run(case)
            return "merged"

        monkeypatch.setattr(runner, "_review_only", review)
        assert runner.review_only(1, case.ctx) == "merged"
        assert waits == [("review", runtime)]
        return

    def execute(**kw):
        _assert_current_run(case)
        if not case.implementer.calls:
            return case.implementer.run(**kw)
        return ExecResult(0, False, "VERDICT: PASS", kw["log_path"])

    case.ctx.executor_factory = lambda _: SimpleNamespace(run=execute)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert waits[0] == (capacity.RUN_KIND, runtime)
    reviewers = [wait for kind, wait in waits[1:] if kind == "review"]
    assert reviewers and all(600 < wait <= runtime for wait in reviewers), waits


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@pytest.mark.parametrize("record", ["dead-pid", "own-pid"])
def test_a_running_record_is_no_reservation_unless_its_pid_is_this_live_process(
        git_history, tmp_path, monkeypatch, record):
    """Admission reads the live slot holders, not the record's status (review finding, #993)."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    capacity.write_state(case.store, 1, case.clock[0])
    pid = _dead_pid() if record == "dead-pid" else os.getpid()
    case.store.update_run(1, pid=pid, process_identities=[])
    assert case.store.get_run(1).status == "running"
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(9, branch="other", worktree="other", pid=os.getpid())  # Another live slot.
    waits, claims = [], []

    def release(seconds):
        waits.append(case.store.get_run(1).pid)  # No claim while it waits.
        sibling.finish_run(9, "merged")

    def execute(*args):
        claims.append(case.store.get_run(1).pid)
        return "merged"

    monkeypatch.setattr(capacity.time, "sleep", release)
    monkeypatch.setattr(runner, "_execute", execute)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert claims == [os.getpid()]
    if record == "dead-pid":
        assert waits and waits[0] == pid, "a dead pid's record started work in an occupied budget"
    else:
        assert not waits, "the run the tick launched this process for must reuse its own slot"


@pytest.mark.parametrize("age,waits", [(timedelta(0), False), (capacity.LAUNCH_GRACE, True)],
                         ids=["fresh-reservation", "reservation-past-grace"])
def test_direct_admission_judges_a_reservation_by_the_clock_that_wrote_it(
        git_history, tmp_path, monkeypatch, age, waits):
    """An overloaded machine holds only new work: the store's own clock tells a reservation from a leftover."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.clock[0] += age  # The wall clock is days past this store's fake clock in both cases.
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", capacity.OVERLOAD_LOAD))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    entered = []

    def admit():
        with capacity.hold_workload(case.store, case.config, wait_s=0, kind=capacity.RUN_KIND, issue=1):
            entered.append(capacity.machine_runs(case.store, case.clock[0]))

    if waits:
        with pytest.raises(TimeoutError):
            admit()
        assert entered == []
    else:
        admit()
        assert entered == [1]  # The reservation and its runner are one slot.
    assert list(capacity_sessions.read(case.store.root)) == []

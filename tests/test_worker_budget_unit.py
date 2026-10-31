"""A direct rerun's own slot covers its first reviewer, as an admitted run's record does (#993)."""

import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import capacity, review_perspectives, runner
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_review_perspectives import perspective_case
from test_board_status_groups import offline_github  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


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
        monkeypatch.setattr(runner, "_review_only", lambda *args: "merged")
        assert runner.review_only(1, case.ctx) == "merged"
        assert waits == [("review", runtime)]
        return

    def execute(**kw):
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

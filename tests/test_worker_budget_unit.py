"""A direct rerun's own slot covers its first reviewer, as an admitted run's record does (#993)."""

from pathlib import Path
import sys
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import capacity, review_perspectives, runner
from foragent.executors import ExecResult
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

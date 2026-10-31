"""Admitted workers reuse capacity; direct workers and Stop hooks cannot bypass it."""

from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from foragent import capacity, runner, run_checks
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_review_perspectives import perspective_case
from test_board_status_groups import offline_github  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

from test_order_machine_budget import fake_workloads  # noqa: F401


@pytest.mark.parametrize("entry", ["issue", "review"])
def test_unadmitted_runner_waits_for_shared_capacity_before_work(git_history, tmp_path, monkeypatch, entry):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "error")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    capacity.write_state(case.store, 1, case.clock[0])
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(9, branch="other", worktree="other", pid=capacity.os.getpid())
    waits, calls = [], []

    def release(seconds):
        assert not calls
        waits.append(seconds)
        sibling.finish_run(9, "merged")

    def execute(*args):
        calls.append(args)
        assert waits, "A direct runner ignored the occupied machine budget"
        assert capacity.machine_runs(case.store, case.clock[0]) == 1
        return "merged"

    monkeypatch.setattr(capacity.time, "sleep", release)
    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_review_only", execute)
    outcome = runner.run_issue(1, case.ctx) if entry == "issue" else runner.review_only(1, case.ctx)
    assert outcome == "merged" and len(calls) == 1
    assert waits and capacity.machine_runs(case.store, case.clock[0]) == 0


def test_stop_hook_check_holds_test_slot_without_double_counting_its_worker(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", test_slots=1)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, *a, **kw: case.store))
    # Match the actual Stop hook argument, which identifies its run and state store.
    snapshot = case.store.root / "approvals/1.json"
    calls = []

    def check(argv, **kw):
        calls.append(argv)
        assert kw.get("env", {}).get(capacity.TEST_SLOT_ENV) == "held", "Stop hook omitted the shared test slot"
        assert capacity.machine_runs(case.store, case.clock[0]) == 1
        return 0, False, ""

    monkeypatch.setattr(runner, "run_logged", check)
    assert run_checks.check_done(snapshot, case.root, case.config.acceptance_dir) == 0
    assert len(calls) == 2


def test_reviewer_fanout_counts_each_simultaneous_session_and_merges_once(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=3, review_max=0, max_retries=1)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", .5))
    capacity.write_state(case.store, 2, case.clock[0])
    lock, pair = Lock(), Barrier(2, timeout=2)
    active, maximum, reviews, merges = [0], [0], [], []

    def review(**kw):
        with lock:
            index = len(reviews)
            if index >= 4:  # #993 (2026-10-31, test owner): one reviewer per perspective (four defaults, #1167)
                merges.append(kw)
                assert active[0] == 0
                return ExecResult(0, False, "VERDICT: PASS", kw["log_path"])
            reviews.append(kw)
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        try:
            assert active[0] <= capacity.machine_runs(case.store, case.clock[0]) <= 2
            if index < 2:
                pair.wait()
            return ExecResult(0, False, "VERDICT: PASS", kw["log_path"])
        finally:
            with lock:
                active[0] -= 1

    def execute(**kw):
        return case.implementer.run(**kw) if not case.implementer.calls else review(**kw)

    case.ctx.executor_factory = lambda _: SimpleNamespace(run=execute)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert maximum[0] == 2 and len(reviews) == 4 and len(merges) == 1
    assert capacity.machine_runs(case.store, case.clock[0]) == 0

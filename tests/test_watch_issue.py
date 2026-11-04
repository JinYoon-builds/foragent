from datetime import UTC, datetime, timedelta
import subprocess

import pytest

from foragent import watch_issue
from foragent.state import StateStore

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    store.clock = clock
    return store


def _step(store, phase, minutes=1):
    """Move the run to ``phase`` a few minutes later, as the runner does (the old phase is journaled)."""
    store.clock[0] += timedelta(minutes=minutes)
    store.update_run(7, phase=phase, phase_started_at=store.clock[0].isoformat())


def _fail_ci(store):
    store.clock[0] += timedelta(minutes=1)
    store.record_flow("ci-failure", issue=7, run_started_at=store.get_run(7).started_at, pr=1)


def test_ci_state_follows_the_current_run(tmp_path):
    store = _store(tmp_path)
    assert watch_issue.ci_state(store, 7) == "none"
    _step(store, "ci")
    assert watch_issue.ci_state(store, 7) == "running"
    _step(store, "merging")
    assert watch_issue.ci_state(store, 7) == "passed", "this run's CI segment closed into merging"
    _step(store, "implementing")
    store.record_flow("ci-failure", issue=7, run_started_at="2000-01-01T00:00:00+00:00", pr=1)
    assert watch_issue.ci_state(store, 7) == "passed", "another run's failure is not this run's"
    _fail_ci(store)
    assert watch_issue.ci_state(store, 7) == "failed", "the newest CI fact wins"
    _step(store, "ci")
    _step(store, "merge-lock-wait")
    assert watch_issue.ci_state(store, 7) == "passed", "a later attempt passed after the failure"
    assert watch_issue.ci_state(store, 8) == "none"


@pytest.mark.parametrize("outcome, expected", [
    ("ci-failed", "failed"),       # retries ran out: finish_run keeps phase "ci"
    ("merge-queued", "passed"),    # handed to the merge owner only after CI passed
    ("merged", "passed"),          # journaled as "ci -> merged"
    ("timeout", "stopped"),        # ended while CI ran: no result, and not running
    ("stopped", "stopped"),
])
def test_a_finished_run_is_judged_by_its_outcome_not_its_last_phase(tmp_path, outcome, expected):
    store = _store(tmp_path)
    _step(store, "ci")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, outcome)
    assert store.get_run(7).phase == "ci", "the premise: the last phase survives finish_run"
    assert watch_issue.ci_state(store, 7) == expected


def test_a_finished_run_after_a_recorded_ci_failure_is_failed(tmp_path):
    store = _store(tmp_path)
    _step(store, "ci")
    _step(store, "implementing")
    _fail_ci(store)
    store.finish_run(7, "retries-exceeded")
    assert watch_issue.ci_state(store, 7) == "failed"


@pytest.mark.parametrize("phase, outcome", [
    ("resolving-conflict", "timeout"),   # resumed WIP against a moved base, timed out before any PR
    ("resolving-conflict", None),        # still resolving: no PR, no CI yet
    ("merging", None),                   # a merge step reached without CI in this run
    ("merge-lock-wait", "stopped"),
])
def test_a_step_name_without_a_ci_segment_in_this_run_is_no_ci_result(tmp_path, phase, outcome):
    store = _store(tmp_path)
    _step(store, phase)
    if outcome:
        store.clock[0] += timedelta(minutes=1)
        store.finish_run(7, outcome)
    assert watch_issue.ci_state(store, 7) == "none"


def test_an_earlier_runs_ci_pass_is_not_this_runs(tmp_path):
    store = _store(tmp_path)
    _step(store, "ci")
    _step(store, "merging")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "crashed")
    store.clock[0] += timedelta(minutes=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    _step(store, "resolving-conflict")
    assert watch_issue.ci_state(store, 7) == "none"


def test_known_and_infer(tmp_path):
    store = _store(tmp_path)
    assert watch_issue.known(store, 7) and not watch_issue.known(store, 8)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "foragent/issue-12", str(repo)], check=True)
    assert watch_issue.infer(repo) == 12
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    assert watch_issue.infer(other) is None
    assert watch_issue.infer(tmp_path / "missing") is None

"""Non-start failures remain visible and do not discard cached approved work."""

from dataclasses import replace
from datetime import timedelta
import json
import os

import pytest

from foragent import capacity, scheduler
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home  # noqa: F401

from test_order_machine_budget import fake_workloads  # noqa: F401


@pytest.mark.parametrize("failed_step", ["github_refresh", "batch_reconcile", "parent_sync"])
def test_non_start_failure_keeps_three_cached_admissions_and_records_the_fact(
    git_history, tmp_path, monkeypatch, failed_step,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "error")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    body = case.tracker.get_issue(1).body
    for number in (2, 3):
        case.tracker.add_issue(number, f"Queued {number}", body)
        case.tracker.record_label(number, "approved", "human")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    assert scheduler.tick(tick_context(case, start_runs=False)).skipped is None
    original_as_of = case.store.issues_observation()["as_of"]
    case.clock[0] += timedelta(minutes=5, seconds=1)
    capacity.write_state(case.store, 3, case.clock[0])
    launched, failures = [], []
    error = f"offline {failed_step} timeout"

    def broken(*args, **kwargs):
        failures.append(failed_step)
        raise TrackerError(error)

    if failed_step == "github_refresh":
        monkeypatch.setattr(case.tracker, "list_issues", broken)
    elif failed_step == "batch_reconcile":
        monkeypatch.setattr(scheduler, "_reconcile_batches", broken)
    else:
        monkeypatch.setattr(scheduler.board_parents, "follow_children", broken)
    context = tick_context(case)
    context.launcher = lambda n: launched.append(n) or capacity.os.getpid()
    report = scheduler.tick(context)
    assert report.skipped is None
    assert launched == [1, 2, 3], "One non-start error discarded this tick's approved starts"
    assert failures == [failed_step], "Do not repeatedly retry the failed auxiliary work"
    assert sorted(run.number for run in case.store.active_runs()) == [1, 2, 3]
    journal = case.store.root / "orch-events.jsonl"
    facts = journal.read_text() if journal.exists() else ""
    assert error in "\n".join(report.actions) + facts, "The tolerated failure must stay observable"
    if failed_step == "github_refresh":
        observed = case.store.issues_observation()
        assert observed["as_of"] == original_as_of
        assert observed["error"] == error and observed["failed_at"] is not None
    # Do not double-admit the same work on the next tick after a transient failure.
    assert scheduler.tick(context).skipped is None
    assert launched == [1, 2, 3]


def test_tick_trims_journals_once_and_direct_log_pruning_still_trims(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    trim = case.store.trim_journals
    calls = []

    def counted(now):
        calls.append(now)
        return trim(now)

    monkeypatch.setattr(case.store, "trim_journals", counted)
    assert scheduler.tick(tick_context(case, start_runs=False)).skipped is None
    assert calls == [case.clock[0]], "Housekeeping and prune_logs parsed the same journals twice"
    case.store.prune_logs(case.clock[0])
    assert calls == [case.clock[0], case.clock[0]], "Direct prune_logs must retain its journal-retention contract"


@pytest.mark.parametrize("changed", ["withdrawn", "closed", "body", "read_failure"])
def test_refresh_failure_rereads_one_issue_and_skips_only_unsafe_candidate(
    git_history, tmp_path, monkeypatch, changed,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "error")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    body = case.tracker.get_issue(1).body
    case.tracker.add_issue(2, "Independent queued work", body)
    case.tracker.record_label(2, "approved", "human")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", .5))
    assert scheduler.tick(tick_context(case, start_runs=False)).skipped is None
    case.clock[0] += timedelta(minutes=5, seconds=1)
    capacity.write_state(case.store, 2, case.clock[0])
    remote = {n: case.tracker.get_issue(n) for n in (1, 2)}
    if changed == "withdrawn":
        remote[1] = replace(remote[1], labels=remote[1].labels - {"approved"})
    elif changed == "closed":
        remote[1] = replace(remote[1], state="closed")
    elif changed == "body":
        remote[1] = replace(remote[1], body=body + "\nChanged scope remotely.\n")
    reads, launches = [], []
    class Remote:
        def __getattr__(self, name):
            return getattr(case.tracker, name)
        def list_issues(self, *a, **kw):
            raise TrackerError("offline listing timeout")
        def get_issue(self, n):
            reads.append(n)
            if n == 1 and changed == "read_failure":
                raise TrackerError("offline single issue unavailable")
            return remote[n]
    context = tick_context(case)
    context.tracker = Remote()
    def launch(n):
        assert reads.count(n) == 1, "Exactly one current issue read is required before fallback start"
        launches.append(n)
        return capacity.os.getpid()
    context.launcher = launch
    report = scheduler.tick(context)
    assert report.skipped is None
    assert reads == [1, 2], "Fallback must bypass cached issue data, without repeated issue GETs"
    assert launches == [2], "Revoked/closed/edited/unreadable work must not start; independent work must"
    assert [r.number for r in case.store.active_runs()] == [2]


def test_optional_parent_and_batch_gh_share_ten_second_deadline(git_history, tmp_path, monkeypatch):
    import subprocess
    import time
    from foragent import tracker as tracker_module
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    elapsed, calls = [0.0], []
    original = subprocess.run
    def run(argv, *args, **kw):
        if argv[0] != "gh":
            return original(argv, *args, **kw)
        timeout = kw.get("timeout")
        calls.append(timeout)
        assert 0 < timeout <= min(5, 10 - elapsed[0]), "Optional gh must receive the remaining deadline"
        elapsed[0] += timeout
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", .5))
    gh = tracker_module.GhTracker("offline/repo")
    def optional(*args, **kwargs):
        for n in range(4):
            gh.sub_issues(n + 1)
    monkeypatch.setattr(scheduler, "_reconcile_batches", optional)
    monkeypatch.setattr(scheduler.board_parents, "follow_children", optional)
    report = scheduler.tick(tick_context(case, start_runs=False))
    assert report.skipped is None
    assert calls == [5, 5] and elapsed[0] == 10, "Both optional steps share one bounded budget"
    assert any("defer" in line.lower() or "timeout" in line.lower() for line in report.actions)

"""No merge, owner refresh or owner check starts during a project stop (#1491).

The merge owner keeps every queued job through the stop and merges it after the release.
Git and GitHub use the offline merge-owner fixtures; no real process or signal.
"""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from acceptance import test_merge_path_real_settings as contract
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from foragent import merge_owner, orch, run_publish
from foragent.runner import run_issue
from foragent.scheduler import TickReport
from test_merge_owner_proto import drain, gate, proto, queue_workers  # noqa: F401
from test_orch_premerge import merge_case  # noqa: F401

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_orch_review_handoff import make_case  # noqa: E402

WHY = "Jin requested a stop"


def _job(case, n):
    (row,) = merge_owner.admissions(case.store).values()
    path = case.store.root / f"merge-owner/{n}.json"
    return {**row["publication"], **(json.loads(path.read_text()) if path.exists() else {})}


def test_the_owner_merges_nothing_during_a_stop_and_keeps_the_job(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    orch.stop_all(case.ctx, WHY)
    for _ in range(3):
        gate(case)
    assert case.tracker.merged_prs == []
    assert len(merge_owner.admissions(case.store)) == 1, "The queued job waits for the release"
    report = TickReport()
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("No owner process starts"))
        case.launch_owner(case.ctx, report)
    assert report.actions == []
    orch.release(case.ctx)
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1


def test_a_stop_during_the_owner_refresh_checks_pushes_nothing(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 11\n"})
    remote = git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-5")
    before = _job(case, 5)
    real = merge_owner._verify_review_head

    def verify(*args, **kwargs):
        orch.stop_all(case.ctx, WHY)  # The stop lands while the refreshed head is checked.
        return real(*args, **kwargs)

    monkeypatch.setattr(merge_owner, "_verify_review_head", verify)
    gate(case)
    monkeypatch.setattr(merge_owner, "_verify_review_head", real)
    assert git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-5") == remote
    after = _job(case, 5)
    assert (after["phase"], after["head"]) == (before["phase"], before["head"]), "The job is kept as it was"
    assert len(merge_owner.admissions(case.store)) == 1 and case.tracker.merged_prs == []
    orch.release(case.ctx)
    drain(case, count=1)
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 11"
    assert len(case.tracker.merged_prs) == 1


def test_a_running_owner_starts_no_refresh_check_after_a_stop(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 11\n"})
    orch.stop_all(case.ctx, WHY)
    monkeypatch.setattr(merge_owner, "_verify_review_head", lambda *a, **k: pytest.fail("No check starts"))
    report = TickReport()
    with case.store.lock_acceptance_baseline("merge-owner", blocking=False):
        merge_owner.tick(case.ctx, report)  # An owner process that started before the stop.
    assert len(merge_owner.admissions(case.store)) == 1 and case.tracker.merged_prs == []


def _stop_during_retitle(monkeypatch, module, ctx):
    real, stops = module.retitle_for_merge, []

    def retitle(*args, **kwargs):
        subject = real(*args, **kwargs)
        if not stops:
            stops.append(orch.stop_all(ctx, WHY))  # Once: after the last merge read, during the retitle.
        return subject

    monkeypatch.setattr(module, "retitle_for_merge", retitle)


def _stopped_with_open_pr(case):
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY)
    assert case.tracker.merged_prs == []
    pr = case.tracker.find_pr("foragent/issue-1")
    assert pr is not None and case.tracker.get_pr(pr).state == "open", "The PR keeps the work for the release"


def test_a_stop_during_the_retitle_merges_nothing_in_a_run(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    _stop_during_retitle(monkeypatch, run_publish, case.ctx)
    assert run_issue(1, case.ctx) == "stopped"
    _stopped_with_open_pr(case)


def test_a_stop_during_the_retitle_merges_nothing_in_a_review_rerun(git_history, tmp_path, monkeypatch):
    from foragent.runner import review_only

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.ctx.config = replace(case.ctx.config, always_human=["feature.txt"])
    assert run_issue(1, case.ctx) != "merged"  # The PR waits for a person, so it stays open.
    pr = case.tracker.find_pr("foragent/issue-1")
    case.ctx.config = replace(case.ctx.config, always_human=[])
    case.clock[0] += timedelta(minutes=5)
    _stop_during_retitle(monkeypatch, run_publish, case.ctx)
    review_only(1, case.ctx, verdict_head=case.tracker.pr_head_sha(pr))
    assert case.tracker.merged_prs == [] and case.tracker.get_pr(pr).state == "open"


def test_a_stop_during_the_owner_retitle_merges_nothing_and_keeps_the_job(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    _stop_during_retitle(monkeypatch, run_publish, case.ctx)
    gate(case)
    assert case.tracker.merged_prs == []
    assert len(merge_owner.admissions(case.store)) == 1, "The queued job waits for the release"
    orch.release(case.ctx)
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1


def test_a_stop_during_the_retitle_refuses_a_person_merge(merge_case, monkeypatch):
    c = merge_case
    _stop_during_retitle(monkeypatch, orch, c.ctx)
    with pytest.raises(orch.OrchError, match="project stop"):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert c.tracker.merged_prs == []


def test_a_run_waiting_for_its_merge_finishes_stopped_with_its_pr_open(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    real = run_publish._merge_lock

    def merge_lock(n, ctx):
        orch.stop_all(case.ctx, WHY)  # The stop lands while the run waits for its merge turn.
        return real(n, ctx)

    monkeypatch.setattr(run_publish, "_merge_lock", merge_lock)
    assert run_issue(1, case.ctx) == "stopped"
    _stopped_with_open_pr(case)

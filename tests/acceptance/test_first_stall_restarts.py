"""Acceptance test for first-stall-restarts. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import subprocess

import pytest

from conftest import git_command
from foragent import runner, scheduler
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


def snapshot(case):
    """Labels plus persisted history; runs.jsonl shows rows read_history() may hide."""
    raw = [json.loads(line) for line in
           (case.store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    return {
        "labels": sorted(case.tracker.get_issue(1).labels & {"running", "ready", "blocked"}),
        "outcomes": [row["outcome"] for row in raw if row["number"] == 1],
    }


def stall_inside_runner(git_history, tmp_path, monkeypatch, *, restarts_before):
    """Run the real runner; its worker stalls and a real tick stops it.

    No real process is signalled: the fake kill_runner raises SystemExit(143)
    inside the runner, the way the scheduler's signal would end it.
    """
    for root, ref in ((git_history.origin, "refs/heads/foragent/issue-1"),
                      (git_history.clone, "refs/heads/foragent/issue-1"),
                      (git_history.clone, "refs/remotes/origin/foragent/issue-1")):
        subprocess.run(["git", "-C", str(root), "update-ref", "-d", ref], capture_output=True)
    git_command(git_history.clone, "fetch", "-q", "origin")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     test_command="true", max_retries=0, max_runtime_minutes=90,
                     stall_minutes=5)
    case.tracker.record_label(1, "approved", "human")
    if restarts_before:
        case.store.update_run(1, restarts=restarts_before)
    scheduler.tick(tick_context(case, start_runs=False))
    signals = []

    def work(kwargs):
        case.clock[0] += timedelta(minutes=case.config.stall_minutes + 1)
        context = tick_context(case, start_runs=False)

        def terminate(pid):
            assert pid == case.store.get_run(1).pid
            signals.append(pid)
            raise SystemExit(143)

        context.kill_runner = terminate
        scheduler.tick(context)
        return "Worker stopped"

    case.implementer.steps = [work]
    with pytest.raises(SystemExit) as exited:
        runner.run_issue(1, case.ctx)
    assert exited.value.code == 143
    assert len(signals) == 1, "The stall tick must stop the runner exactly once"
    assert case.store.get_run(1).restarts == restarts_before + 1
    return case


def settle(case, ticks=2):
    """Let later ticks (no new launches) reconcile the stopped run."""
    for _ in range(ticks):
        case.clock[0] += timedelta(seconds=1)
        scheduler.tick(tick_context(case, start_runs=False))
    return snapshot(case)


def test_first_stall_returns_the_issue_to_ready(git_history, tmp_path, monkeypatch, offline_acceptance):
    case = stall_inside_runner(git_history, tmp_path, monkeypatch, restarts_before=0)
    observed = {"after-runner-exit": snapshot(case), "after-ticks": settle(case)}
    expected = {"labels": ["ready"], "outcomes": ["stalled-restart"]}
    assert observed["after-ticks"] == expected and "crashed" not in observed["after-runner-exit"]["outcomes"], (
        "A first stall must return the issue to ready (not blocked, not running) for its "
        f"one automatic restart, with one stalled-restart row and no crashed row; observed={observed}"
    )


def test_first_stall_is_launched_again_by_the_next_tick(git_history, tmp_path, monkeypatch, offline_acceptance):
    case = stall_inside_runner(git_history, tmp_path, monkeypatch, restarts_before=0)
    settle(case)
    launched = []

    def launcher(number):
        launched.append(number)
        return 7000 + number

    case.clock[0] += timedelta(seconds=1)
    context = tick_context(case)
    context.launcher = launcher
    report = scheduler.tick(context)
    assert launched == [1], (
        "The next tick must start the stalled issue again exactly once; "
        f"labels={sorted(case.tracker.get_issue(1).labels)} actions={report.actions}"
    )
    assert "crashed" not in snapshot(case)["outcomes"], snapshot(case)


def test_second_stall_stays_blocked_for_the_orchestrator(git_history, tmp_path, monkeypatch, offline_acceptance):
    case = stall_inside_runner(git_history, tmp_path, monkeypatch, restarts_before=1)
    observed = {"after-runner-exit": snapshot(case), "after-ticks": settle(case)}
    row = {"labels": ["blocked"], "outcomes": ["stalled"]}
    assert observed == {"after-runner-exit": row, "after-ticks": row}, (
        "A second stall must end stalled and stay blocked (no running, no ready, "
        f"no crashed row); observed={observed}"
    )

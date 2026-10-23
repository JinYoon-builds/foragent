"""Acceptance test for stop-clears-running. Locked by Foragent; do not edit."""

from dataclasses import asdict, is_dataclass
from datetime import timedelta
import json
import shlex

import pytest

from conftest import git_command
from foragent import runner, scheduler
from foragent.state import StateStore
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


RELATED_TEST = "tests/test_stop_probe.py"
STOPS = {
    "timeout": "timeout",
    "related-check-timeout": "timeout",
    "retries-exceeded": "retries-exceeded",
    "spec-question": "spec-question",
    "killed": "killed",
    "stalled": "stalled",
}


def completion(case):
    """Read persisted history too: read_history() can hide duplicate rows."""
    history = [row for row in case.store.read_history() if row.number == 1]
    raw = [json.loads(line) for line in
           (case.store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    raw = [row for row in raw if row["number"] == 1]
    return {
        "labels": sorted(case.tracker.get_issue(1).labels & {"running", "ready", "blocked"}),
        "history": [(row.started_at, row.status, row.outcome) for row in history],
        "runs.jsonl": [(row["started_at"], row["status"], row["outcome"]) for row in raw],
    }


def expected_completion(started_at, outcome, *, labels=("blocked",)):
    row = (started_at, "finished", outcome)
    return {"labels": list(labels), "history": [row], "runs.jsonl": [row]}


@pytest.fixture
def finished_stops(git_history, tmp_path, monkeypatch, offline_acceptance):
    """Exercise real termination paths; replace only workers/check processes.

    Keep the already-working stop paths in the same regression tests as signal
    cleanup, so every collected test is red before the complete contract lands.
    """
    git_history.update({RELATED_TEST: "def test_probe():\n    assert True\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    cases = {}
    for stop, outcome in STOPS.items():
        with monkeypatch.context() as patch:
            case = make_case(
                git_history, tmp_path / stop, patch, skill_libraries=[],
                test_command="python -m pytest -q" if stop == "related-check-timeout" else "true",
                max_retries=0, max_runtime_minutes=90, stall_minutes=5,
            )
            case.tracker.record_label(1, "approved", "human")
            started_at = case.store.get_run(1).started_at
            # Also seed #138's local snapshot, when implemented, with running.
            scheduler.tick(tick_context(case, start_runs=False))

            def work(kwargs):
                if stop == "timeout":
                    case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes)
                elif stop == "related-check-timeout":
                    case.implement(kwargs)
                    (kwargs["worktree"] / RELATED_TEST).write_text(
                        "def test_probe():\n    assert 1 == 1\n", encoding="utf-8",
                    )
                elif stop == "spec-question":
                    question = kwargs["worktree"] / ".foragent/QUESTION.md"
                    question.parent.mkdir(exist_ok=True)
                    question.write_text("Which locale? A: English (recommended); B: Korean.\n")
                elif stop == "killed":
                    raise SystemExit(143)
                elif stop == "stalled":
                    # A second stall is terminal. Deliver the scheduler's stop
                    # signal before its own post-signal cleanup has run, letting
                    # run_issue finish the record in its finally block.
                    case.store.update_run(1, restarts=1)
                    case.clock[0] += timedelta(minutes=case.config.stall_minutes + 1)
                    context = tick_context(case, start_runs=False)

                    def terminate(pid):
                        assert pid == case.store.get_run(1).pid
                        assert case.store.get_run(1).outcome == "stalled"
                        raise SystemExit(143)

                    context.kill_runner = terminate
                    scheduler.tick(context)
                # retries-exceeded: a worker that makes no change exhausts its
                # real attempt budget without a stubbed runner outcome.
                return "Worker stopped"

            case.implementer.steps = [work]
            related_calls = []
            run_logged = runner.run_logged

            def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
                words = shlex.split(argv[2]) if argv[:2] == ["/bin/sh", "-c"] else []
                report = next((word.split("=", 1)[1] for word in words
                               if word.startswith("--junitxml=")), None)
                if report is not None:
                    # No previously passing acceptance tests in this tiny repo.
                    from pathlib import Path

                    Path(report).write_text("<testsuite/>\n", encoding="utf-8")
                    return 0, False, ""
                if RELATED_TEST in words:
                    related_calls.append(timeout_s)
                    case.clock[0] += timedelta(seconds=timeout_s)
                    return -9, True, ""
                return run_logged(argv, cwd=cwd, timeout_s=timeout_s, log_path=log_path, **kwargs)

            patch.setattr(runner, "run_logged", check_process)
            if stop in {"killed", "stalled"}:
                with pytest.raises(SystemExit) as exited:
                    runner.run_issue(1, case.ctx)
                assert exited.value.code == 143
            else:
                assert runner.run_issue(1, case.ctx) == outcome, case.store.log_path(1).read_text()
            assert case.store.get_run(1).status == "finished"
            assert case.store.get_run(1).outcome == outcome
            assert len(case.implementer.calls) == 1
            assert case.tracker.merged_prs == []
            if stop == "related-check-timeout":
                assert len(related_calls) == 1, "The related-check timeout path was not reached"

            immediate = completion(case)
            expected = expected_completion(started_at, outcome)
            # Absence of the API skips only this assertion, never the test.
            reader = getattr(StateStore(case.store.root), "local_issues", None)
            if reader is not None:
                rows = [asdict(row) if is_dataclass(row) else dict(row) for row in reader()]
                local = [row for row in rows if row["number"] == 1]
                immediate["local_running"] = ["running" in row["labels"] for row in local]
                expected["local_running"] = [False]
            cases[stop] = (case, immediate, expected)
    return cases


def test_finished_stops_clear_running_before_runner_exit(finished_stops):
    observed = {stop: immediate for stop, (_, immediate, _) in finished_stops.items()}
    expected = {stop: expected for stop, (_, _, expected) in finished_stops.items()}
    assert observed == expected, (
        "Every runner-finished stop must remove running and retain blocked before exit; "
        f"observed={observed}"
    )


def test_two_ticks_preserve_finished_stops_but_recover_a_dead_runner(
    finished_stops, git_history, tmp_path, monkeypatch,
):
    observed, expected = {}, {}
    for stop, (case, _, wanted) in finished_stops.items():
        context = tick_context(case, start_runs=False)
        snapshots = []
        for _ in range(2):
            # A spurious crash must be visible even with history deduplication.
            case.clock[0] += timedelta(seconds=1)
            scheduler.tick(context)
            snapshots.append(completion(case))
        observed[stop] = snapshots
        expected[stop] = [{key: value for key, value in wanted.items()
                           if key != "local_running"}] * 2

    # Guard real crash recovery in this regression test rather than creating a
    # separate green test: a dead process still has an unfinished run record.
    dead = make_case(git_history, tmp_path / "dead", monkeypatch,
                     skill_libraries=[], test_command="true")
    dead_pid = 987654321
    dead.store.update_run(1, pid=dead_pid)
    started_at = dead.store.get_run(1).started_at
    kill = scheduler.os.kill
    probes = []

    def process_gone(pid, sig):
        if pid == dead_pid:
            assert sig == 0
            probes.append(pid)
            raise ProcessLookupError(pid)
        return kill(pid, sig)

    monkeypatch.setattr(scheduler.os, "kill", process_gone)
    context = tick_context(dead, start_runs=False)
    snapshots = []
    for _ in range(2):
        dead.clock[0] += timedelta(seconds=1)
        scheduler.tick(context)
        snapshots.append(completion(dead))
    assert probes == [dead_pid], "The dead runner must be recovered exactly once"
    observed["dead-runner"] = snapshots
    expected["dead-runner"] = [expected_completion(started_at, "crashed", labels=("ready",))] * 2

    assert observed == expected, (
        "Two ticks must preserve each finished stop as one history row and blocked, "
        "without a synthetic crashed row or promotion to ready; a real dead runner "
        f"must still get exactly one crashed row; observed={observed}"
    )

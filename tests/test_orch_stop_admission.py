"""Every start path asks the one project-stop admission, and a stopped run's exits keep its work (#1491).

Processes and Git use the offline fixtures of the existing scheduler, dream and acceptance tests.
"""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from conftest import git_command  # noqa: E402
from foragent import orch, orch_architect, orch_dream, orch_dream_worker, orch_role_schedule, orch_stop  # noqa: E402
from foragent import run_publish, scheduler  # noqa: E402
from foragent.runner import review_only, run_issue  # noqa: E402
from test_orch_dream_unit import KEY, MEMORY, dreaming, world, worker_world  # noqa: E402,F401
from test_orch_review_handoff import make_case  # noqa: E402
from test_scheduler_flow import _add, _reasons, _tick, lane  # noqa: E402,F401
from test_scheduler import ctx  # noqa: E402,F401

WHY = "Jin requested a stop"


def _control(store, config, now):
    return SimpleNamespace(store=store, config=config, now=now, monotonic=time.monotonic, sleep=time.sleep)


def test_a_project_stop_holds_every_start_of_a_tick_until_its_resume(lane, monkeypatch):
    prebuilds, dreams, roles = [], [], []
    monkeypatch.setattr(scheduler, "prebuild_acceptance_baseline", lambda *a: prebuilds.append(a))
    monkeypatch.setattr(orch_dream, "startable", lambda ctx, sample: ["orch"])
    monkeypatch.setattr(orch_dream, "start", lambda ctx, lanes, budget, report: dreams.append(lanes))
    monkeypatch.setattr(orch_role_schedule, "startable", lambda ctx, sample: ["architect"])
    monkeypatch.setattr(orch_role_schedule, "dispatch", lambda ctx, names, budget, report: roles.append(names))
    _add(lane, 1)
    orch.stop_all(_control(lane.store, lane.config, lane.now), WHY)
    _tick(lane)
    assert (lane.starts, prebuilds, dreams, roles) == ([], [], [], [])
    assert _reasons(lane, 1) == ["project-stop"], "The stop itself is the one admission answer"
    orch.resume_all(_control(lane.store, lane.config, lane.now))
    _tick(lane)
    assert lane.starts == [1] and prebuilds and dreams == [["orch"]] and roles == [["architect"]]


def test_dream_admission_refuses_during_a_stop(world, monkeypatch):
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: ["orch"])
    assert orch_dream._admissible(world.ctx) == ["orch"]
    orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)
    assert orch_dream._admissible(world.ctx) == [], "Rechecked under the machine lock before every dream start"


def test_a_role_session_does_not_start_during_a_stop(tmp_path):
    from foragent.config import Config
    from foragent.state import StateStore
    from datetime import UTC, datetime

    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 11, 3, tzinfo=UTC))
    config = Config(repo="o/r")
    orch.stop_all(_control(store, config, store._now), WHY)
    with pytest.raises(orch_architect.ArchitectRefused, match="project stop is in force"):
        orch_architect.call(tmp_path, store, config, "measure", store._now())


def test_a_dream_worker_started_after_a_stop_takes_no_step(world):
    calls, publish = worker_world(world, lambda worktree: pytest.fail("No executor turn"), lambda: 7)
    dreaming(world)
    orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    dream = world.store.get_dream("orch")
    assert (dream.status, dream.outcome, dream.detail) == ("finished", "stopped", WHY)


def test_a_dream_stopped_during_its_turn_keeps_its_edit_and_publishes_nothing(world, monkeypatch):
    def edit(worktree):
        path = worktree / MEMORY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a lesson\n")
        orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)

    calls, publish = worker_world(world, edit, lambda: pytest.fail("No publication after a stop"))
    monkeypatch.setattr(orch_dream_worker, "_publish", publish)
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    assert calls == {"commits": [[".claude/agent-memory/orch"]], "published": []}
    assert world.store.get_dream("orch").outcome == "stopped"


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    return make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")


def test_a_run_started_during_a_stop_takes_no_step(case, monkeypatch):
    orch.stop_all(case.ctx, WHY)
    case.implementer.steps = []
    fetched = []
    monkeypatch.setattr(case.git, "fetch", lambda *a, **k: fetched.append(a))
    assert run_issue(1, case.ctx) == "stopped"
    assert case.implementer.calls == [] and fetched == [], "No fetch, worktree or baseline run either"
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY)


def test_a_rerun_review_does_not_start_during_a_stop(case):
    orch.stop_all(case.ctx, WHY)
    before = case.store.get_run(1)
    assert review_only(1, case.ctx) == "stopped"
    assert case.store.get_run(1) == before and case.reviewer.calls == []


def test_a_stop_during_review_only_checks_keeps_new_files_and_writes_nothing(case, monkeypatch):
    real = run_publish.retitle_for_merge
    with monkeypatch.context() as first:
        first.setattr(run_publish, "retitle_for_merge", lambda *a, **k: (orch.stop_all(case.ctx, WHY), real(*a, **k))[1])
        assert run_issue(1, case.ctx) == "stopped"  # Opens the PR, then stops before merging.
    pr = case.tracker.find_pr("foragent/issue-1")
    head = case.tracker.pr_head_sha(pr)
    case.tracker.remove_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=5)
    orch.resume_all(case.ctx)
    from foragent import runner

    checks = runner._check

    def check(command, worktree, *args, **kwargs):  # The stop lands while the last check runs.
        result = checks(command, worktree, *args, **kwargs)
        if "acceptance" not in command:
            return result
        (worktree / "review-notes.txt").write_text("written during the review checks\n")
        orch.stop_all(case.ctx, WHY)
        return replace(result, exit_code=1)  # Without the stop it would fail and be flagged.

    monkeypatch.setattr(runner, "_check", check)
    comments = len(case.tracker.comments.get(1, []))
    assert review_only(1, case.ctx, verdict_head=head) == "stopped"
    saved = git_command(case.root, "show", "refs/heads/foragent/issue-1:review-notes.txt")
    assert saved == "written during the review checks", "The owner's WIP save keeps the new file"
    labels = case.tracker.get_issue(1).labels
    assert "running" in labels and "blocked" not in labels, "No failure flag; the tick restores the stop"
    assert len(case.tracker.comments.get(1, [])) == comments, "No comment after the stop"
    assert case.tracker.pr_head_sha(pr) == head, "The open PR keeps its head"
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY)


def test_no_check_command_runs_while_a_stop_is_in_force(case, monkeypatch):
    from contextlib import nullcontext

    from foragent import runner
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_checks_approved import run_done_when

    monkeypatch.setattr(runner, "run_logged", lambda *a, **k: pytest.fail("No check after a stop"))
    orch.stop_all(case.ctx, WHY)  # Between steps: e.g. while the run waited for the merge lock.
    with orch_stop.owner(case.store), pytest.raises(ExecutorStopped):
        run_done_when(1, case.ctx, "true", case.root, case.store.log_path(1), 60, slot=nullcontext())

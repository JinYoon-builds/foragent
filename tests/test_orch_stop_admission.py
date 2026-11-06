"""Every start path asks the one project-stop admission, and a stopped run's exits keep its work (#1491).

Processes and Git use the offline fixtures of the existing scheduler, dream and acceptance tests.
"""

from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace

import pytest


from conftest import git_command  # noqa: E402
from foragent import orch, orch_architect, orch_dream, orch_dream_worker, orch_role_schedule, orch_stop  # noqa: E402
from foragent import run_publish, scheduler  # noqa: E402
from foragent.runner import review_only, run_issue  # noqa: E402
from test_orch_dream_unit import KEY, MEMORY, dreaming, world, worker_world  # noqa: E402,F401
from helpers.review_case import make_case  # noqa: E402
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
    orch.release(_control(lane.store, lane.config, lane.now))
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


def test_a_stop_during_review_only_checks_keeps_new_files(case, monkeypatch):
    case.ctx.config = replace(case.ctx.config, always_human=["feature.txt"])
    assert run_issue(1, case.ctx) != "merged"  # The PR waits for a person, so it stays open.
    pr = case.tracker.find_pr("foragent/issue-1")
    head = case.tracker.pr_head_sha(pr)
    case.clock[0] += timedelta(minutes=5)
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
    review_only(1, case.ctx, verdict_head=head)  # Its failure handling after the stop is a non-goal.
    saved = git_command(case.root, "show", "refs/heads/foragent/issue-1:review-notes.txt")
    assert saved == "written during the review checks", "The worktree removal kept the new file"
    assert case.tracker.pr_head_sha(pr) == head, "The WIP stays local: the open PR keeps its head"


def test_a_role_child_takes_no_turn_after_a_stop(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from foragent.config import Config
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 11, 3, tzinfo=UTC))
    config = Config(repo="o/r")
    orch.stop_all(_control(store, config, store._now), WHY)  # Between `call` and the child's turn.
    monkeypatch.setattr(orch_architect.executors, "executor_for", lambda name: pytest.fail("No role turn"))
    with pytest.raises(orch_architect.ArchitectRefused, match="project stop"):
        orch_architect.run_session(tmp_path, store, config, "measure", "2026-11-03T00:00:00+00:00", store._now)

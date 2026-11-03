"""After a project stop, a run's owner takes no external step and every exit keeps its work (#1491).

One gate (`stop_gate`) sits on each path an external step passes: the run's tracker, remote Git,
check commands after their slot wait, and wait sleeps. Offline fixtures only; no real signal.
"""

from contextlib import contextmanager
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from conftest import git_command  # noqa: E402
from foragent import orch, orch_architect, orch_dream_worker, orch_stop, run_publish, runner, stop_gate  # noqa: E402
from foragent.git import GitRepo  # noqa: E402
from foragent.run_attempt import ExecutorStopped  # noqa: E402
from foragent.runner import review_only, run_issue  # noqa: E402
from test_orch_dream_unit import MEMORY, dreaming, world, worker_world  # noqa: E402,F401
from test_orch_review_handoff import make_case  # noqa: E402

WHY = "Jin requested a stop"


def _control(store, config, now):
    return SimpleNamespace(store=store, config=config, now=now, monotonic=time.monotonic, sleep=time.sleep)


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    return make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")


def test_every_gate_is_silent_outside_an_owner_and_closed_inside_one(case, tmp_path):
    orch.stop_all(case.ctx, WHY)
    calls = []
    tracker = stop_gate.Guarded(SimpleNamespace(comment=lambda *a: calls.append(a), name="fake"))
    git = GitRepo(tmp_path, lambda args: calls.append(args) or "")
    ctx = orch_stop.guarded(SimpleNamespace(store=case.store, tracker=tracker, sleep=lambda s: calls.append(s)))
    tracker.comment(1, "outside")  # Another process (the tick, a person's command) is not gated.
    git._call(["push", "origin", "x"])
    assert len(calls) == 2
    with orch_stop.owner(case.store):
        for step in (lambda: tracker.comment(1, "inside"), lambda: git._call(["-C", "wt", "push", "origin", "x"]),
                     lambda: git._call(["fetch", "origin"]), lambda: git._call(["ls-remote", "origin"]),
                     lambda: ctx.sleep(30)):
            with pytest.raises(ExecutorStopped):
                step()
        git._call(["status"])  # Local Git stays open: the save commits through it.
        assert tracker.name == "fake"
        with stop_gate.saving():
            git._call(["push", "origin", "x"])
            tracker.comment(1, "the save's own reads and push")
    assert len(calls) == 5


def test_a_check_waiting_for_its_test_slot_does_not_run_after_a_stop(case, monkeypatch):
    @contextmanager
    def slot():  # The stop lands while the check waits for a machine slot.
        orch.stop_all(case.ctx, WHY)
        yield

    monkeypatch.setattr(runner, "run_logged", lambda *a, **k: pytest.fail("No check after its slot wait"))
    with orch_stop.owner(case.store), pytest.raises(ExecutorStopped):
        runner._check("true", case.root, case.store.log_path(1), 60, slot=slot())


def test_a_stop_during_the_publication_push_opens_no_pr(case, monkeypatch):
    real = case.git._run

    def git_run(args):
        if "push" in args:
            orch.stop_all(case.ctx, WHY)  # The push lands; the stop arrives while it runs.
        return real(args)

    monkeypatch.setattr(case.git, "_run", git_run)
    assert run_issue(1, case.ctx) == "stopped"
    assert case.tracker.find_pr("foragent/issue-1") is None, "No PR after the stop"
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY)


def test_a_stopped_run_leaves_every_wait_at_its_next_poll(case, monkeypatch):
    orch.stop_all(case.ctx, WHY)
    polls = []
    ctx = orch_stop.guarded(case.ctx)
    with orch_stop.owner(case.store):
        with pytest.raises(ExecutorStopped):
            for _ in range(3):  # The shape of every CI, merge-turn and overlap wait: poll, then sleep.
                polls.append(1)
                ctx.sleep(60)
    assert polls == [1]


def test_a_failed_save_still_finishes_a_stopped_review(case, monkeypatch):
    from datetime import timedelta

    real = run_publish.retitle_for_merge
    with monkeypatch.context() as first:
        first.setattr(run_publish, "retitle_for_merge", lambda *a, **k: (orch.stop_all(case.ctx, WHY), real(*a, **k))[1])
        assert run_issue(1, case.ctx) == "stopped"
    head = case.tracker.pr_head_sha(case.tracker.find_pr("foragent/issue-1"))
    case.tracker.remove_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=5)
    orch.resume_all(case.ctx)

    def review(kwargs):
        orch.stop_all(case.ctx, WHY)
        return "VERDICT: PASS"

    case.reviewer.steps = [review]
    monkeypatch.setattr(runner, "_save_review_worktree", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert review_only(1, case.ctx) == "stopped"
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY), "The finish still runs"
    assert "WIP preservation failed: OSError: disk full" in case.store.log_path(1).read_text()


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


def test_a_stop_during_dream_preparation_starts_no_turn(world, monkeypatch):
    calls, publish = worker_world(world, lambda worktree: pytest.fail("No dream turn"), lambda: 7)
    prepare = orch_dream_worker._dream_worktree

    def dream_worktree(ctx, lane, worktree):
        orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)
        return prepare(ctx, lane, worktree)

    monkeypatch.setattr(orch_dream_worker, "_dream_worktree", dream_worktree)
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    assert world.store.get_dream("orch").outcome == "stopped"


def _memory(worktree):
    path = worktree / MEMORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a lesson\n")


def test_a_stop_during_the_dream_commit_publishes_nothing(world, monkeypatch):
    calls, publish = worker_world(world, _memory, lambda: pytest.fail("No publication after a stop"))
    monkeypatch.setattr(orch_dream_worker, "_publish", publish)
    git = world.ctx.git.at(None)
    commit = git.commit_paths
    git.commit_paths = lambda paths, title: (commit(paths, title), orch.stop_all(
        _control(world.store, world.config, world.ctx.now), WHY))
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    assert calls == {"commits": [[".claude/agent-memory/orch"]], "published": []}
    dream = world.store.get_dream("orch")
    assert (dream.outcome, dream.detail) == ("stopped", WHY), "Not recorded as dreamed"


def test_a_stop_before_the_media_file_publishes_nothing(world, monkeypatch):
    def edit(worktree):
        _memory(worktree)
        proposal = worktree / ".foragent" / "media-orch.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text("media\n")
        orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)

    calls, publish = worker_world(world, edit, lambda: pytest.fail("No publication after a stop"))
    monkeypatch.setattr(orch_dream_worker, "publish_media", lambda *a: pytest.fail("No media after a stop"))
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    assert calls["commits"] == [[".claude/agent-memory/orch"]], "The edit is kept on the dream branch"


def test_a_dream_that_fails_after_a_stop_keeps_its_edit_and_finishes_stopped(world):
    def edit(worktree):
        _memory(worktree)
        orch.stop_all(_control(world.store, world.config, world.ctx.now), WHY)
        raise RuntimeError("the executor crashed after the stop")

    calls, publish = worker_world(world, edit, lambda: pytest.fail("No publication after a stop"))
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 1
    assert calls["commits"] == [[".claude/agent-memory/orch"]]
    dream = world.store.get_dream("orch")
    assert (dream.status, dream.outcome) == ("finished", "stopped")


def test_a_check_stopped_while_it_runs_leaves_no_result(case, monkeypatch):
    def command(*a, **k):  # The stop lands while the command runs; it then fails.
        orch.stop_all(case.ctx, WHY)
        return 1, False, ""

    monkeypatch.setattr(runner, "run_logged", command)
    with orch_stop.owner(case.store), pytest.raises(ExecutorStopped):
        runner._check("true", case.root, case.store.log_path(1), 60)


def test_any_failure_after_a_stop_ends_the_run_as_stopped_without_a_flag(case):
    def implement(kwargs):
        case.implement(kwargs)
        orch.stop_all(case.ctx, WHY)
        raise RuntimeError("the executor crashed after the stop")

    case.implementer.steps = [implement]
    assert run_issue(1, case.ctx) == "stopped"
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert git_command(case.root, "show", "refs/heads/foragent/issue-1:feature.txt") == "done"


def test_any_failure_after_a_stop_ends_a_review_rerun_as_stopped(case, monkeypatch):
    from datetime import timedelta

    real = run_publish.retitle_for_merge
    with monkeypatch.context() as first:
        first.setattr(run_publish, "retitle_for_merge", lambda *a, **k: (orch.stop_all(case.ctx, WHY), real(*a, **k))[1])
        assert run_issue(1, case.ctx) == "stopped"
    case.tracker.remove_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=5)
    orch.resume_all(case.ctx)

    def review(kwargs):
        orch.stop_all(case.ctx, WHY)
        raise RuntimeError("the reviewer crashed after the stop")

    case.reviewer.steps = [review]
    assert review_only(1, case.ctx) == "stopped"
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert case.store.get_run(1).outcome == "stopped"

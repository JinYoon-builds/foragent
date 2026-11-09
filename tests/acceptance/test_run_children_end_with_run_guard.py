"""Regression guard for run-children-end-with-run (not a Done when file; passes on main).

Statement 3 control: when no process works inside the issue worktree, dead-runner recovery
ends nothing else. Processes outside the worktree keep running and get no signal, and the
recovery itself still deletes the worktree and returns the issue to ready.
"""

import pytest

from run_children_world import make_world


@pytest.fixture
def world(git_history, tmp_path, monkeypatch, fake_process_table):
    return make_world(git_history, tmp_path, monkeypatch, fake_process_table)


def test_recovery_without_leftovers_leaves_outside_processes_running(world):
    world.start_running()
    path = world.worktree()
    sibling = world.worktree(10)
    sibling.mkdir(parents=True)
    outside = {7001: world.clone, 7002: sibling, 7003: world.store.root, 7004: path.parent}
    for pid, cwd in outside.items():
        world.other(pid, cwd=cwd)
    world.runner_dies()

    _, error = world.tick()

    assert error is None, f"gate recovery failed: {error!r}"
    for pid in outside:
        assert world.alive(pid), f"process {pid} outside the worktree was ended"
        assert world.signals_to(pid) == []
    assert world.stale_signals == []
    assert not path.exists()
    assert world.store.get_run(1).outcome == "crashed"
    assert world.ctx.tracker.get_issue(1).labels == {"approved", "ready"}

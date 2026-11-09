"""Acceptance: a run's test processes never outlive it or block its worktree's cleanup (PM r1456).

Incident: #2506 was blocked retries-exceeded with "failed to delete worktrees/issue-2506:
Directory not empty". An orphan `fa test` loop (parent 1) from a dead runner kept its
working directory in the worktree for 1h43m and every retry failed the same way.

Statements:
1. When a run's runner dies and the gate recovers the run, a detached process the run
   started (parent 1, working directory inside the issue worktree) no longer runs.
2. Deleting an issue worktree succeeds when a leftover process had its working directory
   inside it; that process is ended first.
3. A process whose working directory is outside the issue worktree is not ended by that
   cleanup.

All processes live in the fake process table; no real process is signalled.
"""

from datetime import timedelta

import pytest

from run_children_world import NOW, RUNNER_PID, make_world


LOOP, LOOP_CHILD = 5555, 5556


@pytest.fixture
def world(git_history, tmp_path, monkeypatch, fake_process_table):
    return make_world(git_history, tmp_path, monkeypatch, fake_process_table)


@pytest.mark.parametrize("where", ["root", "subdirectory"])
def test_gate_recovery_of_dead_runner_ends_its_detached_test_loop(world, where):
    """Statement 1: the orphan loop and its `fa test` child stop with the recovered run."""
    run = world.start_running()
    path = world.worktree()
    cwd = path if where == "root" else path / "tests"
    assert cwd.is_dir()
    # The implementer session (a child of the runner) detached a test loop; the runner then died.
    world.detached_loop(LOOP, parent=RUNNER_PID, cwd=cwd, child=LOOP_CHILD)
    world.runner_dies()
    assert world.table.rows[LOOP].ppid == 1

    _, error = world.tick()

    assert not world.alive(LOOP), "the detached test loop of the dead run still runs after gate recovery"
    assert not world.alive(LOOP_CHILD), "the loop's `fa test` child still runs after gate recovery"
    assert error is None, f"gate recovery failed: {error!r}"
    assert world.store.get_run(1).status == "finished"
    assert world.store.get_run(1).outcome == "crashed"
    assert run.started_at == world.store.get_run(1).started_at
    assert not path.exists(), "the issue worktree was not deleted"
    assert world.ctx.tracker.get_issue(1).labels == {"approved", "ready"}, "the run was not recovered to ready"
    # Ended once: no signal goes to a process that already ended, now or on a later tick.
    assert world.stale_signals == [], f"signals sent to ended processes: {world.stale_signals}"
    sent = list(world.table.signals)
    world.tick()
    assert world.table.signals == sent
    assert world.stale_signals == []


def test_recovery_deletes_worktree_held_by_leftover_process_and_retry_starts_clean(world):
    """Statement 2 (dead-runner recovery): the leftover process is ended before the delete.

    The world's git refuses to delete a directory a live process still works in, as it did
    for worktrees/issue-2506, so a clean delete proves the process ended first.
    """
    world.start_running()
    path = world.worktree()
    world.detached_loop(LOOP, parent=RUNNER_PID, cwd=path / "tests")
    world.runner_dies()
    _, error = world.tick()

    assert error is None, f"worktree cleanup failed: {error!r}"
    assert world.removal_failures == [], f"git refused the delete: {world.removal_failures}"
    assert not path.exists(), "the issue worktree is still on disk"
    assert str(path) not in world.ctx.git._call(["worktree", "list"])
    assert not world.alive(LOOP), "the leftover process was not ended"
    assert world.stale_signals == []
    assert world.store.get_run(1).status == "finished"
    assert world.ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    # A retry finds no leftover worktree: a fresh one can be created at the same path.
    world.ctx.git.add_worktree(path, "foragent/issue-1-retry", "origin/main")
    assert path.is_dir()


def test_stall_kill_deletes_worktree_held_by_leftover_process(world):
    """Statement 2 (live runner stopped by the gate): the leftover is ended, the delete succeeds."""
    world.start_running()
    path = world.worktree()
    # The loop left the runner's group and session, so stopping the runner alone misses it.
    world.detached_loop(LOOP, parent=1, cwd=path)
    world.ctx.now = lambda: NOW + timedelta(minutes=world.ctx.config.stall_minutes + 1)

    _, error = world.tick()

    assert not world.alive(RUNNER_PID)
    assert not world.alive(LOOP), "the leftover process in the worktree was not ended"
    assert error is None, f"worktree cleanup failed: {error!r}"
    assert world.removal_failures == []
    assert not path.exists()
    assert world.store.get_run(1).status == "finished"
    assert world.store.get_run(1).outcome == "stalled-restart"


def test_cleanup_ends_only_processes_inside_the_issue_worktree(world):
    """Statement 3: outside processes survive the same cleanup that ends the inside one."""
    world.start_running()
    path = world.worktree()
    store_root = world.store.root
    sibling = world.worktree(10)  # worktrees/issue-10: shares the "issue-1" prefix.
    sibling.mkdir(parents=True)
    other_issue = world.worktree(2)
    other_issue.mkdir(parents=True)
    outside = {
        6001: world.clone,                 # The user's checkout (repository root).
        6002: sibling,                     # A path that only shares the name prefix.
        6003: other_issue,                 # Another issue's worktree.
        6004: store_root,                  # The state directory that holds the worktrees.
        6005: path.parent,                 # The worktrees directory itself.
    }
    for pid, cwd in outside.items():
        world.other(pid, cwd=cwd)
    world.detached_loop(LOOP, parent=RUNNER_PID, cwd=path)
    world.runner_dies()

    _, error = world.tick()

    assert not world.alive(LOOP), "the process inside the issue worktree was not ended"
    assert error is None, f"worktree cleanup failed: {error!r}"
    for pid in outside:
        assert world.alive(pid), f"process {pid} with working directory {world.table.rows[pid].cwd} was ended"
        assert world.signals_to(pid) == [], f"process {pid} outside the worktree was signalled"
    assert not path.exists()
    assert sibling.is_dir() and other_issue.is_dir()

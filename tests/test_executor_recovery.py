"""Executor ownership survives a runner that cannot execute its finally block."""

from datetime import UTC, datetime, timedelta
import signal
from types import SimpleNamespace

import pytest


from foragent.config import Config
from foragent.executors import executor_for
from foragent.processes import observe_tree, run_logged, track_process_group
from foragent.reasons import Reason
from foragent.runner import _run_executor
from foragent.scheduler import TickContext, TickReport, _recover_runs
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture
def recovery(tmp_path):
    now = datetime(2026, 10, 22, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    worktree = store.worktree_path(1)
    worktree.mkdir(parents=True)
    store.start_run(1, branch="foragent/issue-1", worktree=worktree)
    tracker = FakeTracker(now=lambda: now, strict=True)
    tracker.add_issue(1, "Recover executor", labels=["running"])
    return TickContext(
        repo="owner/repo", repo_root=tmp_path, tracker=tracker, store=store,
        # The stall and configured runtime-limit paths (#435 keeps both); a day-long
        # silence window keeps the watchdog from ending these runs first.
        config=Config(silence_minutes=24 * 60),
        git=SimpleNamespace(remove_worktree=lambda path: path.rmdir()),
        now=lambda: now, launcher=lambda number: None, start_runs=False,
    )


def test_crash_recovery_terminates_real_executor_in_its_own_session(recovery, fake_process_table):
    ctx, table = recovery, fake_process_table
    root = table.add(42000, cwd=ctx.repo_root, start_time=10)
    executor = table.add(42001, ppid=root.pid, cwd=ctx.repo_root, start_time=11)
    child = table.add(42002, ppid=executor.pid, cwd=ctx.repo_root, start_time=12)
    ctx.store.update_run(1, pid=root.pid, executor_pgid=executor.pid,
                         process_identities=observe_tree(roots=(root.pid,)))
    table.exit(root.pid)  # A crash bypasses finally; the executor gets reparented.

    def remove_after_executor_exit(path):
        assert not {executor.pid, child.pid} & table.live
        path.rmdir()

    ctx.git.remove_worktree = remove_after_executor_exit
    ctx.store = StateStore(ctx.store.root)
    _recover_runs(ctx, TickReport())
    assert not ctx.store.worktree_path(1).exists()
    assert ctx.store.get_run(1).executor_pgid is None
    assert ctx.store.get_run(1).outcome == "crashed"
    assert ctx.tracker.get_issue(1).labels == {"ready"}


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("timeout", [False, True])
def test_runner_persists_and_clears_real_executor_group(recovery, monkeypatch, fake_process_table, executor, timeout):
    from process_fakes import install_popen

    ctx, observed = recovery, []
    update_run = ctx.store.update_run

    def record(number, **fields):
        result = update_run(number, **fields)
        # Stdin now starts before recording. Check persistence at its callback,
        # without requiring the executor to wait for that callback to read stdin.
        if fields.get("executor_pgid") is not None:
            observed.append(StateStore(ctx.store.root).get_run(number).executor_pgid)
            assert observed[-1] == fields["executor_pgid"]
        return result

    monkeypatch.setattr(ctx.store, "update_run", record)
    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["offline-executor"],
                  timeout=timeout)
    monkeypatch.setattr(f"foragent.executors.{executor}_command", lambda *args: ["offline-executor"])
    if timeout:
        # Past its budget a session ends only once quiet (#435): this one is quiet, so it is stopped.
        monkeypatch.setattr("foragent.processes._watch", lambda *args, **kwargs: True)
    runner_ctx = SimpleNamespace(store=ctx.store, now=ctx.now, poll_hook=lambda execute, poll: execute())
    result = _run_executor(
        1, runner_ctx, executor_for(executor), worktree=ctx.store.worktree_path(1),
        prompt="offline", model=None, timeout_s=2, log_path=ctx.store.log_path(1),
    )
    assert observed and observed[0] != fake_process_table.own_pgid
    assert result.timed_out is timeout
    assert result.exit_code == (-signal.SIGKILL if timeout else 0)
    assert StateStore(ctx.store.root).get_run(1).executor_pgid is None
    assert observed[0] not in fake_process_table.live


def test_persistence_failure_terminates_the_spawned_process(recovery, monkeypatch, fake_process_table):
    from process_fakes import install_popen

    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["offline-executor"])
    groups = []

    def record(pgid):
        groups.append(pgid)
        if pgid is not None:
            raise OSError("cannot persist executor group")

    with track_process_group(record), pytest.raises(OSError, match="cannot persist executor group"):
        run_logged(["offline-executor"], cwd=recovery.repo_root, timeout_s=10, log_path=recovery.store.log_path(1))
    assert len(groups) == 2 and groups[-1] is None
    assert groups[0] not in fake_process_table.live


def test_permission_denied_termination_is_not_retried_by_logged_cleanup(recovery, monkeypatch, fake_process_table):
    from foragent import processes
    from process_fakes import install_popen

    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["offline-executor"], timeout=True)
    attempts = []

    def denied(pid, signum):
        attempts.append((pid, signum))
        raise PermissionError("not ours")

    monkeypatch.setattr(processes.os, "kill", denied)
    with pytest.raises(PermissionError, match="not ours"):
        run_logged(["offline-executor"], cwd=recovery.repo_root, timeout_s=1,
                   log_path=recovery.store.log_path(1), terminate_children=True)
    assert attempts == [(42000, signal.SIGTERM)]


@pytest.mark.parametrize("trigger", ["crash", "stall", "timeout"])
@pytest.mark.parametrize("failure", [False, True])
def test_gate_cleans_executor_before_worktree_for_every_termination(recovery, fake_process_table, trigger, failure):
    ctx, table = recovery, fake_process_table
    root = table.add(42000, cwd=ctx.repo_root, start_time=10)
    table.add(54321, ppid=root.pid, cwd=ctx.repo_root, start_time=11)
    ctx.store.update_run(1, pid=root.pid, executor_pgid=54321,
                         process_identities=observe_tree(roots=(root.pid,)))
    if trigger == "crash":
        table.exit(root.pid)
    else:
        elapsed = ctx.config.stall_minutes + 1 if trigger == "stall" else ctx.config.max_runtime_minutes + 11
        now = ctx.now() + timedelta(minutes=elapsed)
        ctx.now = lambda: now
    actions = []

    def kill(pgid):
        actions.append(pgid)
        if failure and pgid == 54321:
            raise TimeoutError("executor still alive")

    ctx.kill_runner = kill
    if failure:
        _recover_runs(ctx, TickReport())
        assert ctx.store.get_run(1).status == "running"
        assert ctx.store.get_run(1).executor_pgid == 54321
        assert ctx.store.get_run(1).restarts == 0
        assert ctx.store.worktree_path(1).exists()
        assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED
        assert "executor still alive" in ctx.store.get_run(1).detail
        _recover_runs(ctx, TickReport())  # A refusal must not retry signals next tick.
    else:
        def remove(path):
            assert actions == [root.pid, 54321]
            path.rmdir()

        ctx.git.remove_worktree = remove
        _recover_runs(ctx, TickReport())
        assert ctx.store.get_run(1).status == "finished"
        assert ctx.store.get_run(1).executor_pgid is None
        assert ctx.store.get_run(1).outcome == {
            "crash": "crashed", "stall": "stalled-restart", "timeout": "killed",
        }[trigger]
    assert actions == [root.pid, 54321]

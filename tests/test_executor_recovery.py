"""Executor ownership survives a runner that cannot execute its finally block."""

import ctypes
from datetime import UTC, datetime, timedelta
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

REAL_SIGNALS_SKIP = pytest.mark.skip(reason='Sends real signals to real process groups; group numbers can be reused by other processes (PermissionError seen 2026-10-24). Rewrite on a fake process table with #261.')

from foragent.config import Config
from foragent.executors import executor_for
from foragent.processes import run_logged, track_process_group
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
        config=Config(), git=SimpleNamespace(remove_worktree=lambda path: path.rmdir()),
        now=lambda: now, launcher=lambda number: None, start_runs=False,
    )


@pytest.fixture
def reap_orphans():
    # Minimal Linux containers may have a PID 1 that never reaps zombies.
    # Adopt the orphan so the real killer's waitpid can reap it, as init would.
    if sys.platform != "linux":
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    try:
        yield
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@REAL_SIGNALS_SKIP
def test_crash_recovery_terminates_real_executor_in_its_own_session(recovery, reap_orphans):
    ctx = recovery
    ready = ctx.repo_root / "executor-ready"
    child_script = """
from pathlib import Path
import sys, time
Path(sys.argv[1]).touch()
time.sleep(300)
"""
    parent_script = """
from pathlib import Path
import sys
from foragent.processes import run_logged, track_process_group
from foragent.state import StateStore
store = StateStore(Path(sys.argv[1]))
with track_process_group(lambda pgid: store.update_run(1, executor_pgid=pgid)):
    run_logged(
        [sys.executable, '-c', sys.argv[2], sys.argv[3]],
        cwd=store.worktree_path(1), timeout_s=300, log_path=store.log_path(1),
    )
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(ctx.store.root), child_script, str(ready)],
        start_new_session=True,
    )
    pgid = None
    try:
        ctx.store.update_run(1, pid=parent.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pgid = ctx.store.get_run(1).executor_pgid
            if ready.exists() and pgid is not None:
                break
            assert parent.poll() is None, "runner exited before spawning executor"
            time.sleep(0.01)
        assert ready.exists() and pgid is not None
        assert os.getpgid(pgid) == pgid != os.getpgid(parent.pid)
        parent.kill()  # SIGKILL bypasses run_logged cleanup and leaves the child alive.
        parent.wait(timeout=5)
        os.kill(pgid, 0)

        def remove_after_executor_exit(path):
            with pytest.raises(ProcessLookupError):
                os.kill(pgid, 0)
            path.rmdir()

        ctx.git.remove_worktree = remove_after_executor_exit
        # A fresh store must load the ownership recorded by the dead runner.
        ctx.store = StateStore(ctx.store.root)
        _recover_runs(ctx, TickReport())

        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
        assert not ctx.store.worktree_path(1).exists()
        assert ctx.store.get_run(1).executor_pgid is None
        assert ctx.store.get_run(1).outcome == "crashed"
        assert ctx.tracker.get_issue(1).labels == {"ready"}
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        pgid = pgid or ctx.store.get_run(1).executor_pgid
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if sys.platform == "linux":
                try:
                    os.waitpid(pgid, 0)
                except ChildProcessError:
                    pass


@REAL_SIGNALS_SKIP
@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("timeout", [False, True])
def test_runner_persists_and_clears_real_executor_group(recovery, monkeypatch, executor, timeout):
    ctx = recovery
    observed = ctx.repo_root / "observed-pgid"
    script = """
from pathlib import Path
import json, os, sys, time
deadline = time.monotonic() + 5
while json.loads(Path(sys.argv[1]).read_text()).get('executor_pgid') != os.getpgrp():
    assert time.monotonic() < deadline, 'executor group was not persisted'
    time.sleep(0.01)
Path(sys.argv[2]).write_text(str(os.getpgrp()))
if sys.argv[3] == 'timeout':
    time.sleep(300)
"""
    argv = [sys.executable, "-c", script, str(ctx.store.root / "runs/1.json"),
            str(observed), "timeout" if timeout else "exit"]
    monkeypatch.setattr(f"foragent.executors.{executor}_command", lambda *args: argv)
    runner_ctx = SimpleNamespace(store=ctx.store, now=ctx.now, poll_hook=lambda execute, poll: execute())

    result = _run_executor(
        1, runner_ctx, executor_for(executor), worktree=ctx.store.worktree_path(1),
        prompt="offline", model=None, timeout_s=2 if timeout else 10, log_path=ctx.store.log_path(1),
    )

    pgid = int(observed.read_text())
    assert pgid != os.getpgrp()
    assert result.timed_out is timeout
    assert result.exit_code == (-signal.SIGKILL if timeout else 0)
    assert StateStore(ctx.store.root).get_run(1).executor_pgid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pgid, 0)


@REAL_SIGNALS_SKIP
def test_persistence_failure_terminates_the_spawned_process(recovery):
    groups = []

    def record(pgid):
        groups.append(pgid)
        if pgid is not None:
            raise OSError("cannot persist executor group")

    with track_process_group(record), pytest.raises(OSError, match="cannot persist executor group"):
        run_logged(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            cwd=recovery.repo_root, timeout_s=10, log_path=recovery.store.log_path(1),
        )
    assert len(groups) == 2 and groups[-1] is None
    with pytest.raises(ProcessLookupError):
        os.killpg(groups[0], 0)


@REAL_SIGNALS_SKIP
@pytest.mark.parametrize("trigger", ["crash", "stall", "timeout"])
@pytest.mark.parametrize("failure", [False, True])
def test_gate_cleans_executor_before_worktree_for_every_termination(recovery, trigger, failure):
    ctx = recovery
    if trigger == "crash":
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait(timeout=5)
        pid = process.pid
    else:
        pid = os.getpid()
        elapsed = ctx.config.stall_minutes + 1 if trigger == "stall" else ctx.config.max_runtime_minutes + 11
        now = ctx.now() + timedelta(minutes=elapsed)
        ctx.now = lambda: now
    ctx.store.update_run(1, pid=pid, executor_pgid=54321)
    actions = []

    def kill(pgid):
        actions.append(pgid)
        if failure and pgid == 54321:
            raise TimeoutError("executor still alive")

    ctx.kill_runner = kill
    if failure:
        with pytest.raises(TimeoutError, match="executor still alive"):
            _recover_runs(ctx, TickReport())
        assert ctx.store.get_run(1).status == "running"
        assert ctx.store.get_run(1).executor_pgid == 54321
        assert ctx.store.get_run(1).restarts == 0
        assert ctx.store.worktree_path(1).exists()
    else:
        def remove(path):
            assert actions == [pid, 54321]
            path.rmdir()

        ctx.git.remove_worktree = remove
        _recover_runs(ctx, TickReport())
        assert ctx.store.get_run(1).status == "finished"
        assert ctx.store.get_run(1).executor_pgid is None
        assert ctx.store.get_run(1).outcome == {
            "crash": "crashed", "stall": "stalled-restart", "timeout": "killed",
        }[trigger]
    assert actions == [pid, 54321]

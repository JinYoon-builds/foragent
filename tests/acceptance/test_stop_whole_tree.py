"""Acceptance test for stop-whole-tree. Locked by Foragent; do not edit."""

from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from foragent.executors import ExecResult
from foragent.processes import kill_process_group, run_logged
from foragent.runner import run_issue
from foragent.scheduler import tick
from test_orch_review_handoff import make_case, tick_context


def _alive(pid):
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux containers may leave reparented zombies unreaped. macOS launchd
    # reaps them; neither a process-list command nor an optional dependency is needed.
    if sys.platform.startswith("linux"):
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except FileNotFoundError:
            return False
    return True


def _wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _context(case):
    ctx = tick_context(case, start_runs=False)
    # tick_context deliberately supplies a no-op killer; exercise the gate's real one.
    ctx.kill_runner = kill_process_group
    return ctx


def _case(git_history, tmp_path, monkeypatch):
    return make_case(
        git_history, tmp_path, monkeypatch, executor="codex", reviewer="none",
        skill_libraries=[], test_command="true", max_runtime_minutes=2, stall_minutes=1,
    )


# runner -> executor -> helper -> detached sleep. Only the last process changes
# session and leaves the worktree here, so ownership must survive both escapes;
# run_logged creates the executor session and records its PGID.
_HELPER = """
import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,
                         cwd=Path(sys.argv[1]).parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(json.dumps({
    'executor': os.getppid(), 'helper': os.getpid(), 'detached': child.pid,
    'executor_pgid': os.getpgrp(), 'detached_pgid': os.getpgid(child.pid),
}))
child.wait()
"""
_EXECUTOR = """
import subprocess, sys
subprocess.Popen([sys.executable, '-B', '-c', sys.argv[1], sys.argv[2]]).wait()
"""


class _TreeExecutor:
    def __init__(self, ready):
        self.ready = ready

    def run(self, **kwargs):
        code, timed_out, message = run_logged(
            [sys.executable, "-B", "-c", _EXECUTOR, _HELPER, str(self.ready)],
            cwd=kwargs["worktree"], timeout_s=kwargs["timeout_s"], log_path=kwargs["log_path"],
        )
        return ExecResult(code, timed_out, message, kwargs["log_path"])


@contextmanager
def _running_tree(case, tmp_path):
    ready = tmp_path / "tree.json"
    executor = _TreeExecutor(ready)
    case.ctx.executor_factory = lambda name: executor
    pid = os.fork()
    if pid == 0:
        os.setsid()
        try:
            run_issue(1, case.ctx)
        finally:
            os._exit(0)

    tree = {}

    def started():
        try:
            tree.update(json.loads(ready.read_text()))
        except (FileNotFoundError, ValueError):
            return False
        return case.store.get_run(1).executor_pgid == tree["executor"]

    try:
        log = case.store.log_path(1)
        assert _wait_until(started, seconds=10), (
            "Fake executor did not start through run_issue: "
            + (log.read_text() if log.exists() else "no runner log")
        )
        assert case.store.get_run(1).pid == pid
        assert tree["executor_pgid"] == tree["executor"]
        assert tree["detached_pgid"] == tree["detached"]
        assert tree["detached_pgid"] != tree["executor_pgid"]
        assert all(_alive(child) for child in (pid, tree["executor"], tree["helper"], tree["detached"]))
        yield pid, tree
    finally:
        # Test cleanup is independent of the production killer, even on failure.
        if ready.exists():
            try:
                tree.update(json.loads(ready.read_text()))
            except ValueError:
                pass
        for key in ("detached", "helper", "executor"):
            if key in tree and _alive(tree[key]):
                _kill(tree[key])
        if _alive(pid):
            _kill(pid)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass  # The real killer may already have reaped the runner.
        assert _wait_until(lambda: all(not _alive(tree[key]) for key in
                                      ("detached", "helper", "executor") if key in tree)), tree


@pytest.mark.parametrize("reason", ["time-limit", "stall"])
def test_gate_stop_kills_detached_executor_descendants(git_history, tmp_path, monkeypatch, reason):
    case = _case(git_history, tmp_path, monkeypatch)
    with _running_tree(case, tmp_path) as (runner, tree):
        minutes = case.config.max_runtime_minutes + 11 if reason == "time-limit" else case.config.stall_minutes + 1
        case.clock[0] += timedelta(minutes=minutes)
        report = tick(_context(case))
        run = case.store.get_run(1)
        assert run.status == "finished", report.actions
        assert run.outcome == ("killed" if reason == "time-limit" else "stalled-restart"), run
        assert _wait_until(lambda: not _alive(tree["detached"])), (
            f"{reason}: detached grandchild survived gate stop (left executor process group)"
        )
        assert all(not _alive(pid) for pid in (runner, tree["executor"], tree["helper"])), tree


@pytest.fixture
def orphan_processes(tmp_path):
    """Launch real reparented processes, and always clean up our exact PIDs."""
    pids = []

    def spawn(argv, cwd):
        launcher = """
import json, subprocess, sys
child = subprocess.Popen(json.loads(sys.argv[1]), cwd=sys.argv[2],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", launcher, json.dumps(argv), str(cwd)],
            cwd=tmp_path, check=True, capture_output=True, text=True, timeout=5,
        )
        pid = int(result.stdout.strip())
        pids.append(pid)
        assert _alive(pid), f"Helper {pid} failed to start"
        return pid

    yield spawn
    for pid in pids:
        if _alive(pid):
            _kill(pid)
    assert _wait_until(lambda: all(not _alive(pid) for pid in pids)), pids


def _inactive_and_running(case, orphan_processes, tmp_path, *, inactive):
    case.store.finish_run(1, "killed")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.close_issue(1)
    if inactive != 1:
        case.tracker.add_issue(inactive, "Never launched", labels=["blocked"])
        assert case.store.get_run(inactive) is None
    case.tracker.add_issue(2, "Still working", labels=["running"])
    for number in (inactive, 2):
        case.git.add_worktree(case.store.worktree_path(number), f"foragent/issue-{number}", "origin/main")
    outside = tmp_path / "unrelated"
    outside.mkdir()

    def spawn_in(number, evidence):
        worktree = case.store.worktree_path(number)
        directory = worktree / "helpers"
        directory.mkdir(exist_ok=True)
        if evidence == "cwd":
            # No worktree path in argv: a command-line-only sweep misses this.
            return orphan_processes(["/bin/sleep", "60"], directory)
        script = directory / "worker.py"
        script.write_text("import time\ntime.sleep(60)\n")
        # No worktree cwd: a cwd-only sweep misses this.
        return orphan_processes([sys.executable, "-B", str(script)], outside)

    protected = [spawn_in(2, evidence) for evidence in ("cwd", "argv")]
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2), pid=protected[0])
    protected.append(orphan_processes(["/bin/sleep", "60"], outside))
    # A textual path prefix does not establish worktree ownership.
    lookalike = Path(str(case.store.worktree_path(inactive)) + "-unrelated")
    lookalike.mkdir()
    protected.append(orphan_processes(["/bin/sleep", "60"], lookalike))
    return spawn_in, protected


@pytest.mark.parametrize("inactive", [1, 3], ids=["finished", "never-started"])
@pytest.mark.parametrize("evidence", ["cwd", "argv"])
def test_tick_reaps_inactive_worktree_processes_and_preserves_other_processes(
    git_history, tmp_path, monkeypatch, orphan_processes, inactive, evidence,
):
    case = _case(git_history, tmp_path, monkeypatch)
    spawn_in, protected = _inactive_and_running(case, orphan_processes, tmp_path, inactive=inactive)
    orphan = spawn_in(inactive, evidence)
    tick(_context(case))
    # Check these first so an over-broad cleanup fails visibly too.
    assert all(_alive(pid) for pid in protected), "tick stopped a running issue or an outside process"
    assert case.store.get_run(2).status == "running"
    assert _wait_until(lambda: not _alive(orphan)), (
        f"tick left an inactive issue's {evidence}-matched worktree process alive"
    )


def test_tick_records_issue_and_unique_reaped_process_count(git_history, tmp_path, monkeypatch, orphan_processes):
    case = _case(git_history, tmp_path, monkeypatch)
    spawn_in, protected = _inactive_and_running(case, orphan_processes, tmp_path, inactive=1)
    script = case.store.worktree_path(1) / "worker.py"
    script.write_text("import time\ntime.sleep(60)\n")
    # The first matches both cwd and argv, and must only be counted once.
    orphans = [orphan_processes([sys.executable, "-B", str(script)], script.parent), spawn_in(1, "argv")]
    tick(_context(case))
    assert all(_alive(pid) for pid in protected), "tick stopped a running issue or an outside process"
    path = case.store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    reaped = [event for event in events if event["kind"] == "orphans-reaped" and event["issue"] == 1]
    assert reaped, "tick emitted no kind=orphans-reaped event for issue #1"
    # The existing event schema stores details in summary; a structured count is
    # equally acceptable. Require a complete numeric token, not part of a PID.
    assert (any(event.get("count") == 2 for event in reaped)
            or any("2" in re.findall(r"\b\d+\b", event["summary"]) for event in reaped)), reaped
    assert not any(event["kind"] == "orphans-reaped" and event["issue"] == 2 for event in events), events
    assert _wait_until(lambda: all(not _alive(pid) for pid in orphans)), "reported orphans are still alive"
    # A second tick must not report the same exits again.
    tick(_context(case))
    later = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(_alive(pid) for pid in protected), "second tick stopped a protected process"
    assert [event for event in later if event["kind"] == "orphans-reaped"] == [
        event for event in events if event["kind"] == "orphans-reaped"
    ]

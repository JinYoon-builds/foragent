import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from foragent import processes
from foragent.processes import ProcessInfo, kill_process_group, worktree_processes


def _table(*rows):
    return {pid: ProcessInfo(pid, ppid, pgid, os.getuid(), tuple(argv)) for pid, ppid, pgid, argv in rows}


def test_group_stop_freezes_then_kills_descendants_that_left_the_group(monkeypatch):
    # 100 leads the group; 101 is a member; 102 left it via setsid; 103 is 102's child.
    table = _table((100, 1, 100, []), (101, 100, 100, []), (102, 101, 102, []), (103, 102, 102, []),
                   (200, 1, 200, []))
    calls = []
    monkeypatch.setattr(processes, "process_table", lambda: table)
    monkeypatch.setattr(processes, "_own_lineage", lambda: set())
    monkeypatch.setattr(processes, "process_tree_mark", lambda pid: None)
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))

    def killpg(pid, sig):
        calls.append(("killpg", pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(processes.os, "killpg", killpg)
    monkeypatch.setattr(processes.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(processes.time, "sleep", lambda seconds: None)
    kill_process_group(100)
    stops = {pid for kind, pid, sig in calls if kind == "kill" and sig == signal.SIGSTOP}
    kills = {pid for kind, pid, sig in calls if kind == "kill" and sig == signal.SIGKILL}
    assert stops == kills == {102, 103}
    first_group_signal = calls.index(("killpg", 100, signal.SIGTERM))
    assert all(calls.index(("kill", pid, signal.SIGSTOP)) < first_group_signal for pid in stops)
    assert all(calls.index(("kill", pid, signal.SIGKILL)) > first_group_signal for pid in kills)


def test_group_stop_never_signals_the_caller_lineage(monkeypatch):
    table = _table((100, 1, 100, []), (102, 100, 102, []))
    monkeypatch.setattr(processes, "process_table", lambda: table)
    monkeypatch.setattr(processes, "_own_lineage", lambda: {102})
    monkeypatch.setattr(processes, "process_tree_mark", lambda pid: None)
    sent = []
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError))
    kill_process_group(100)
    assert sent == []


def test_worktree_match_uses_path_boundaries_cwd_and_argv(tmp_path, monkeypatch):
    root = tmp_path / "worktrees" / "issue-1"
    table = _table(
        (10, 1, 10, ["python", str(root / "x.py")]),
        (11, 1, 11, ["tool", f"--dir={root}"]),
        (12, 1, 12, ["sleep", "60"]),  # cwd inside
        (13, 1, 13, ["python", f"{root}-unrelated/x.py"]),
        (14, 1, 14, ["sleep", "60"]),  # lookalike cwd
        (15, 1, 15, ["python", str(root / "x.py")]),  # protected
        (16, 15, 16, ["sleep", "60"]),  # descendant of protected
    )
    cwds = {12: str(root / "sub"), 14: f"{root}-unrelated", 16: str(root)}
    monkeypatch.setattr(processes, "process_table", lambda: table)
    monkeypatch.setattr(processes, "_process_cwd", cwds.get)
    monkeypatch.setattr(processes, "_own_lineage", lambda: set())
    assert worktree_processes({1: root}, protected={15}) == {1: {10, 11, 12}}


def test_process_table_reads_this_process():
    table = processes.process_table()
    me = table[os.getpid()]
    assert (me.ppid, me.pgid, me.uid) == (os.getppid(), os.getpgrp(), os.getuid())
    assert processes.process_argv(me)
    assert Path(processes._process_cwd(os.getpid())).resolve() == Path.cwd().resolve()


def test_group_stop_waits_through_eperm_from_exiting_group(monkeypatch):
    # macOS answers EPERM for a group whose last member is still exiting.
    probes = iter([PermissionError, PermissionError, ProcessLookupError])
    monkeypatch.setattr(processes, "_snapshot_tree", lambda pid: processes.TreeSnapshot(set(), set(), set()))

    def killpg(pid, sig):
        if sig == 0:
            raise next(probes)
        if sig == signal.SIGKILL:
            raise PermissionError

    monkeypatch.setattr(processes.os, "killpg", killpg)
    monkeypatch.setattr(processes.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(processes.time, "sleep", lambda seconds: None)
    kill_process_group(100)


_SPAWN_ON_TERM = """
import os, signal, subprocess, sys, time
from pathlib import Path

def stop(signum, frame):
    child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(sys.argv[1]).write_text(str(child.pid))
    os._exit(0)  # The parent is gone at once, so parent links cannot find the child.

signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1] + '.ready').touch()
time.sleep(60)
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_group_stop_kills_descendant_started_by_a_termination_handler(tmp_path, monkeypatch):
    spawned = tmp_path / "spawned"
    env = {**os.environ, processes.TREE_MARK: "test-tree-mark"}
    process = subprocess.Popen([sys.executable, "-B", "-c", _SPAWN_ON_TERM, str(spawned)],
                               start_new_session=True, env=env)
    child = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "spawned.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "spawned.ready").exists()
        assert processes.process_tree_mark(process.pid) == "test-tree-mark"
        real_sleep = time.sleep
        monkeypatch.setattr(processes.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.5)))
        kill_process_group(process.pid)
        child = int(spawned.read_text())
        deadline = time.monotonic() + 3
        while _alive(child) and time.monotonic() < deadline:
            real_sleep(0.05)
        assert not _alive(child), "a child started after the snapshot survived the stop"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if child is not None and _alive(child):
            os.kill(child, signal.SIGKILL)


def test_run_logged_marks_each_executor_tree(tmp_path):
    out = tmp_path / "mark"
    script = f"import os; open({str(out)!r}, 'w').write(os.environ.get({processes.TREE_MARK!r}, ''))"
    first = []
    for _ in range(2):
        processes.run_logged([sys.executable, "-c", script], cwd=tmp_path, timeout_s=10, log_path=tmp_path / "log")
        first.append(out.read_text())
    assert all(first) and first[0] != first[1]


def test_group_stop_spares_a_gate_tick_the_runner_kicked(monkeypatch):
    # 100 is the runner; 102 is the tick it kicked (own session); 103 is a runner that tick launched.
    table = {
        100: ProcessInfo(100, 1, 100, os.getuid(), ("python", "-m", "foragent.cli", "gate", "run-issue", "1"), 1, 0),
        101: ProcessInfo(101, 100, 101, os.getuid(), ("codex",), 2, 1),
        102: ProcessInfo(102, 100, 102, os.getuid(), ("python", "-m", "foragent.cli", "gate", "tick"), 3, 1),
        103: ProcessInfo(103, 102, 103, os.getuid(), ("python", "-m", "foragent.cli", "gate", "run-issue", "2"), 4, 3),
        104: ProcessInfo(104, 1, 104, os.getuid(), ("sleep",), 5, 2),  # left by 101 after it exited
    }
    killed = []
    monkeypatch.setattr(processes, "process_table", lambda: table)
    monkeypatch.setattr(processes, "_own_lineage", lambda: set())
    monkeypatch.setattr(processes, "process_tree_mark", lambda pid: None)
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError))
    kill_process_group(100)
    assert {pid for pid, sig in killed if sig == signal.SIGKILL} == {101, 104}

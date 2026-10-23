import os
from pathlib import Path
import signal

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
    monkeypatch.setattr(processes, "_escaped_tree", lambda pid: set())

    def killpg(pid, sig):
        if sig == 0:
            raise next(probes)
        if sig == signal.SIGKILL:
            raise PermissionError

    monkeypatch.setattr(processes.os, "killpg", killpg)
    monkeypatch.setattr(processes.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(processes.time, "sleep", lambda seconds: None)
    kill_process_group(100)

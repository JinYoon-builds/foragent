"""Deterministic process I/O shared by unit tests; never signals real PIDs."""

from dataclasses import dataclass, replace
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

@dataclass(frozen=True)
class FakeProcess:
    pid: int
    ppid: int
    pgid: int
    sid: int
    cwd: Path
    start_time: float
    argv: tuple[str, ...] = ()
    marker: str | None = None


class _ModuleView:
    """Replace module I/O locally; the real os signal functions stay forbidden."""

    def __init__(self, module, **overrides):
        self.module, self.overrides = module, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.module, name)


class FakeProcessTable:
    """Model process I/O only. Ownership and kill decisions belong to production.

    #261 needs an enumeration seam: processes.list_processes() returns these
    immutable rows. Inject it even on main, where no enumerator exists yet, so
    failures describe missing behavior rather than an AttributeError. Run
    creation/update must record the observed start time before a PID is reused.
    """

    self_pid, parent_pid, grandparent_pid = 91003, 91002, 91001
    own_pgid, own_sid = 91000, 90999
    unsafe_targets = {
        "init": 1, "self": self_pid, "parent": parent_pid,
        "grandparent": grandparent_pid, "group-leader": own_pgid,
        "session-leader": own_sid, "same-group": 91004, "same-session": 91005,
    }

    def __init__(self, outside):
        self.rows = {}
        self.dead = set()
        self.calls = []
        self.signals = []
        self.leaderless_group_calls = []
        self.denied_groups = set()
        self.after_signal = None
        self.ignore_term = set()
        self.list_calls = 0
        for pid, ppid, pgid, sid in (
            (1, 0, 1, 1), (self.own_sid, 1, self.own_sid, self.own_sid),
            (self.own_pgid, 1, self.own_pgid, self.own_sid),
            (self.grandparent_pid, 1, self.own_pgid, self.own_sid),
            (self.parent_pid, self.grandparent_pid, self.own_pgid, self.own_sid),
            (self.self_pid, self.parent_pid, self.own_pgid, self.own_sid),
            (91004, 1, self.own_pgid, self.own_sid), (91005, 1, 91005, self.own_sid),
        ):
            self.add(pid, ppid=ppid, pgid=pgid, sid=sid, cwd=outside, start_time=0)
        self.host_pids = self.live

    @property
    def live(self):
        return set(self.rows) - self.dead

    def add(self, pid, *, ppid=1, pgid=None, sid=None, cwd, start_time, argv=(), marker=None):
        row = FakeProcess(pid, ppid, pid if pgid is None else pgid,
                          pid if sid is None else sid, Path(cwd), start_time, tuple(argv), marker)
        self.rows[pid] = row
        self.dead.discard(pid)
        return row

    def list_processes(self):
        self.list_calls += 1
        return tuple(self.rows[pid] for pid in sorted(self.live))

    def process(self, pid):
        pid = self.self_pid if pid == 0 else pid
        if pid not in self.live:
            raise ProcessLookupError(pid)
        return self.rows[pid]

    def exit(self, pid):
        self.dead.add(pid)
        for child in tuple(self.live):
            if self.rows[child].ppid == pid:
                self.rows[child] = replace(self.rows[child], ppid=1)

    def _signal(self, kind, target, signum, members):
        call = (kind, target, signum)
        self.calls.append(call)
        if signum:
            # Count attempts too, including attempts against nonexistent groups.
            self.signals.append(call)
        if kind == "pgid" and signum == 0 and target in self.denied_groups:
            raise PermissionError(target)
        if not members:
            raise ProcessLookupError(target)
        if signum:
            for pid in members:
                if signum != signal.SIGTERM or pid not in self.ignore_term:
                    self.exit(pid)
            if self.after_signal is not None:
                self.after_signal(call)

    def kill(self, pid, signum):
        self._signal("pid", pid, signum, {pid} & self.live)

    def killpg(self, pgid, signum):
        if pgid not in self.live:
            self.leaderless_group_calls.append((pgid, signum))
        self._signal("pgid", pgid, signum,
                     {pid for pid in self.live if self.rows[pid].pgid == pgid})

    def waitpid(self, pid, options):
        if pid in self.live:
            return 0, 0
        raise ChildProcessError(pid)



@pytest.fixture
def fake_process_table(monkeypatch, tmp_path, no_real_signals):
    from foragent import capacity, cli, processes, runner, scheduler

    table = FakeProcessTable(tmp_path)
    fake_os = _ModuleView(
        os, kill=table.kill, killpg=table.killpg, waitpid=table.waitpid,
        getpid=lambda: table.self_pid, getppid=lambda: table.parent_pid,
        getpgrp=lambda: table.own_pgid,
        getpgid=lambda pid: table.process(pid).pgid,
        getsid=lambda pid: table.process(pid).sid,
    )
    for module in (processes, scheduler, capacity, runner, cli):
        monkeypatch.setattr(module, "os", fake_os)
    monkeypatch.setattr(processes, "list_processes", table.list_processes)
    monkeypatch.setattr(processes, "time", _ModuleView(time, sleep=lambda seconds: None))
    return table


def install_popen(monkeypatch, table, *, matches, exit_code=0, timeout=False, children=(), communicate=None):
    """Replace only the selected executor; offline Git can still run normally."""
    from foragent import processes

    original = subprocess.Popen

    class Process:
        pid = 42000
        returncode = None

        def __init__(self, argv, **kwargs):
            self.kwargs = kwargs
            self.calls = 0
            table.add(self.pid, cwd=kwargs["cwd"], start_time=10)
            for pid in children:
                table.add(pid, ppid=self.pid, cwd=kwargs["cwd"], start_time=11)
            if timeout:
                table.ignore_term.update((self.pid, *children))

        def poll(self):
            if self.pid not in table.live and self.returncode is None:
                self.returncode = -next(sig for kind, target, sig in reversed(table.signals) if target == self.pid)
            return self.returncode

        def communicate(self, **kwargs):
            self.calls += 1
            if communicate is not None:
                communicate(self)
            if timeout and self.calls == 1:
                raise subprocess.TimeoutExpired("fake executor", 1)
            if self.poll() is None:
                table.exit(self.pid)
                self.returncode = exit_code
            return None, None

        def wait(self, **kwargs):
            assert self.poll() is not None, "Fake process survived cleanup"
            return self.returncode

    def launch(argv, *args, **kwargs):
        if matches(argv):
            return Process(argv, **kwargs)
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(processes.subprocess, "Popen", launch)
    return Process

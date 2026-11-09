"""Offline world for run-children-end-with-run (PM r1456).

The real gate tick, state store and Git worktrees against local Git and a fake
GitHub. Processes exist only in the shared fake process table (tests/process_fakes.py):
no real process is started for the run or its children and no real signal is sent.

Process seams this world serves from one table, so the implementation may read a
process's working directory and end it through any of them:

* ``foragent.processes.list_processes()`` rows (``FakeProcess`` rows carry ``cwd``);
* ``psutil.Process(pid)`` / ``psutil.process_iter()`` (``cwd()``, ``ppid()``,
  ``create_time()``, ``terminate()``, ``kill()``, ``send_signal()``...);
* ``os.kill`` / ``os.killpg`` as seen by foragent.processes, scheduler, capacity,
  runner and cli (patched by the ``fake_process_table`` fixture).

Git models the incident: ``git worktree remove`` of a directory that a live process
still uses as its working directory fails with "Directory not empty", as it did for
worktrees/issue-2506 while an orphan `fa test` loop kept writing caches there.
"""

from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import signal
import subprocess

import psutil
import pytest

from conftest import ACCEPTANCE, git_command
from foragent.config import Config
from foragent.git import GitRepo
from foragent.processes import kill_process_group
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
RUNNER_PID = 4321
BODY = f"""<!-- foragent:v1 -->
## Goal
Implement the feature.
## Out of scope
Other features.
## Done when
```sh
pytest {ACCEPTANCE}
```
"""

_Uids = namedtuple("_Uids", "real effective saved")


def inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


class FakePsutil:
    """psutil.Process / process_iter answered from the fake process table."""

    def __init__(self, table):
        self.table = table
        world = self

        class Process:
            def __init__(self, pid=None):
                pid = table.self_pid if pid is None else pid
                if pid not in table.live:
                    raise psutil.NoSuchProcess(pid)
                self._pid = pid
                self._start = table.rows[pid].start_time
                self.info = {}

            def _row(self):
                if self._pid not in table.live or table.rows[self._pid].start_time != self._start:
                    raise psutil.NoSuchProcess(self._pid)
                return table.rows[self._pid]

            @property
            def pid(self):
                return self._pid

            def ppid(self):
                return self._row().ppid

            def create_time(self):
                return self._row().start_time

            def cmdline(self):
                return list(self._row().argv)

            def name(self):
                argv = self._row().argv
                return Path(argv[0]).name if argv else ""

            def cwd(self):
                return str(self._row().cwd)

            def status(self):
                self._row()
                return psutil.STATUS_SLEEPING

            def uids(self):
                self._row()
                return _Uids(os.getuid(), os.getuid(), os.getuid())

            def username(self):
                self._row()
                return "tester"

            def is_running(self):
                return self._pid in table.live and table.rows[self._pid].start_time == self._start

            def parent(self):
                ppid = self._row().ppid
                return Process(ppid) if ppid in table.live and ppid > 0 else None

            def parents(self):
                chain, current = [], self.parent()
                while current is not None:
                    chain.append(current)
                    current = current.parent()
                return chain

            def children(self, recursive=False):
                self._row()
                found, frontier = [], [self._pid]
                while frontier:
                    parent = frontier.pop()
                    for pid in sorted(table.live):
                        if table.rows[pid].ppid == parent and pid != parent:
                            found.append(Process(pid))
                            if recursive:
                                frontier.append(pid)
                return found

            def open_files(self):
                self._row()
                return []

            def send_signal(self, signum):
                self._row()
                table.kill(self._pid, signum)

            def terminate(self):
                self.send_signal(signal.SIGTERM)

            def kill(self):
                self.send_signal(signal.SIGKILL)

            def wait(self, timeout=None):
                if self.is_running():
                    raise psutil.TimeoutExpired(timeout or 0, self._pid)
                return 0

            @contextmanager
            def oneshot(self):
                yield

            def as_dict(self, attrs=None, ad_value=None):
                names = attrs or ("pid", "ppid", "cwd", "cmdline", "create_time", "status", "name")
                values = {}
                for name in names:
                    try:
                        value = getattr(self, name)
                        values[name] = value() if callable(value) else value
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        values[name] = ad_value
                return values

            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                raise psutil.AccessDenied(getattr(self, "_pid", None))

            def __eq__(self, other):
                return isinstance(other, Process) and (other._pid, other._start) == (self._pid, self._start)

            def __hash__(self):
                return hash((self._pid, self._start))

        def process_iter(attrs=None, ad_value=None):
            for pid in sorted(table.live):
                try:
                    process = Process(pid)
                except psutil.NoSuchProcess:
                    continue
                if attrs is not None:
                    process.info = process.as_dict(attrs, ad_value)
                yield process

        world.Process = Process
        world.process_iter = process_iter
        world.pids = lambda: sorted(table.live)
        world.pid_exists = lambda pid: pid in table.live

    def install(self, monkeypatch):
        for name in ("Process", "process_iter", "pids", "pid_exists"):
            monkeypatch.setattr(psutil, name, getattr(self, name))


@dataclass
class World:
    ctx: TickContext
    table: object
    clone: Path
    removal_failures: list
    stale_signals: list

    @property
    def store(self) -> StateStore:
        return self.ctx.store

    def worktree(self, number: int = 1) -> Path:
        return self.store.worktree_path(number)

    def approve(self, number: int = 1) -> None:
        self.ctx.tracker.add_issue(number, f"Issue {number}", BODY)
        self.ctx.tracker.record_label(number, "approved", "human")

    def start_running(self, number: int = 1, pid: int = RUNNER_PID):
        """A gate-started run: runner process recorded, its issue worktree on disk."""
        self.approve(number)
        tick(self.ctx)  # Snapshot the approval and mark the issue ready.
        self.ctx.tracker.remove_labels(number, ["ready"])
        self.ctx.tracker.add_labels(number, ["running"])
        self.table.add(pid, ppid=1, pgid=pid, sid=pid,
                       cwd=self.clone, start_time=NOW.timestamp(), argv=("fa", "gate", "run-issue", str(number)))
        run = self.store.start_run(number, branch=f"foragent/issue-{number}",
                                   worktree=self.worktree(number), pid=pid)
        self.store.update_run(number, pid=pid, observe=True)  # Launch-time witness of the runner.
        self.ctx.git.add_worktree(self.worktree(number), run.branch, "origin/main")
        return self.store.get_run(number)

    def detached_loop(self, pid: int, *, parent: int, cwd: Path, start: float | None = None, child: int | None = None):
        """`sh -c 'while ...; done | xargs fa test'` started by the session in its own session.

        Started after the last gate observation, so no tick ever witnessed it; its
        `fa test` child shares its group. Both keep their working directory at ``cwd``.
        """
        start = NOW.timestamp() + 60 if start is None else start
        self.table.add(pid, ppid=parent, pgid=pid, sid=pid, cwd=cwd, start_time=start,
                       argv=("sh", "-c", "while true; do ls tests/test_merge_owner*.py | xargs fa test; done"))
        if child is not None:
            self.table.add(child, ppid=pid, pgid=pid, sid=pid, cwd=cwd, start_time=start + 1,
                           argv=("fa", "test", "tests/test_merge_owner.py"))

    def other(self, pid: int, *, cwd: Path, start: float | None = None):
        """An unrelated detached process (parent 1, own session)."""
        self.table.add(pid, ppid=1, pgid=pid, sid=pid, cwd=cwd,
                       start_time=NOW.timestamp() + 30 if start is None else start, argv=("sleep", "86400"))

    def runner_dies(self, pid: int = RUNNER_PID) -> None:
        self.table.exit(pid)  # Its children are reparented to 1, as the kernel does.

    def tick(self):
        """One gate tick; a production exception is returned, not raised, so tests assert on it."""
        try:
            return tick(self.ctx), None
        except Exception as exc:  # noqa: BLE001 - the assertion names it.
            return None, exc

    def signals_to(self, pid: int) -> list:
        """Delivering signals that could reach ``pid``: by PID, or by its process group."""
        row = self.table.rows[pid]
        return [call for call in self.table.signals
                if (call[0] == "pid" and call[1] == pid) or (call[0] == "pgid" and call[1] == row.pgid)]

    def alive(self, pid: int) -> bool:
        return pid in self.table.live


def make_world(git_history, tmp_path, monkeypatch, table) -> World:
    removal_failures, stale_signals = [], []
    deliver = table._signal

    def noting(kind, target, signum, members):
        # A delivering signal to a process (or group) that already ended is a doubled stop:
        # with a reused PID it would reach a stranger.
        if signum and not members:
            stale_signals.append((kind, target, signum))
        return deliver(kind, target, signum, members)

    monkeypatch.setattr(table, "_signal", noting)

    def git_run(args):
        root, command = git_history.clone, args
        while command[:1] == ["-C"]:
            root, command = Path(command[1]), command[2:]
        if command[:1] == ["-c"] and "commit" in command or command[:1] == ["commit"]:
            pytest.fail(f"This world keeps worktrees clean; no WIP commit is expected: {args!r}")
        if command[:1] == ["push"]:
            branch = command[-1]
            git_command(git_history.origin, "fetch", str(root), f"+refs/heads/{branch}:refs/heads/{branch}")
            return ""
        if command[:2] == ["worktree", "remove"]:
            target = Path(command[-1])
            users = sorted(pid for pid in table.live if inside(table.rows[pid].cwd, target))
            if users:
                removal_failures.append((str(target), users))
                raise subprocess.CalledProcessError(
                    255, ["git", *args], output="",
                    stderr=f"error: failed to delete '{target}': Directory not empty\n")
        return GitRepo(git_history.clone)._run_git(args)

    store = StateStore(tmp_path / "state")
    ctx = TickContext(
        repo="owner/repo", repo_root=git_history.clone,
        tracker=FakeTracker(strict=True), store=store,
        config=Config(approvers=["human"]), git=GitRepo(git_history.clone, git_run), now=lambda: NOW,
        launcher=lambda number: 7000 + number, start_runs=False,
        runner_process_exists=lambda number: False,
        kill_runner=kill_process_group,
    )
    ctx.tracker._now = lambda: ctx.now()
    store._now = lambda: ctx.now()
    FakePsutil(table).install(monkeypatch)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: ("normal", 0.0))
    return World(ctx, table, git_history.clone, removal_failures, stale_signals)

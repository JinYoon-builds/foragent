"""Acceptance test for stop-whole-tree. Locked by Foragent; do not edit."""

from dataclasses import dataclass, replace
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from foragent import capacity, cli, processes, runner, scheduler
from test_orch_review_handoff import make_case, tick_context


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


@pytest.fixture(autouse=True)
def fake_processes(monkeypatch, tmp_path, no_forbidden_git_commands):
    """Fail closed before fixtures run: no real signals, forks, ps or helpers.

    Only offline fixture Git may reach Popen. Module-local os views send to the
    fake table; calling the actual os.kill/killpg/pthread_kill is a test failure,
    including signal 0. No real function capable of signaling is retained here.
    """
    table = FakeProcessTable(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail(f"Real process/signal I/O is forbidden in stop tests: {args!r}")

    popen = subprocess.Popen  # Includes conftest's forbidden-Git-command guard.

    def offline_git_only(argv, *args, **kwargs):
        if (not isinstance(argv, (list, tuple)) or not argv or argv[0] != "git"
                or kwargs.get("shell")):
            pytest.fail(f"Only offline fixture git may start; refused {argv!r}")
        cwd = Path(kwargs.get("cwd") or Path.cwd()).resolve()
        if not cwd.is_relative_to(tmp_path.resolve()):
            pytest.fail(f"Git escaped its fixture: {cwd}")
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", offline_git_only)
    for name in ("kill", "killpg", "fork", "forkpty"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(signal, "pthread_kill", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    # No external transport or user/system Git hooks can run in fixture Git.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    for index, (key, value) in enumerate((
        ("core.hooksPath", str(tmp_path / "no-hooks")),
        ("protocol.allow", "never"), ("protocol.file.allow", "always"),
    )):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)

    fake_os = _ModuleView(
        os, kill=table.kill, killpg=table.killpg, waitpid=table.waitpid,
        getpid=lambda: table.self_pid, getppid=lambda: table.parent_pid,
        getpgrp=lambda: table.own_pgid,
        getpgid=lambda pid: table.process(pid).pgid,
        getsid=lambda pid: table.process(pid).sid,
    )
    for module in (processes, scheduler, capacity, runner, cli):
        monkeypatch.setattr(module, "os", fake_os)
    monkeypatch.setattr(processes, "list_processes", table.list_processes, raising=False)
    monkeypatch.setattr(processes, "time", _ModuleView(time, sleep=lambda seconds: None))
    monkeypatch.setattr(scheduler, "machine_load", lambda: capacity.LoadSample("normal", 0, 0))
    return table


def stop_case(git_history, tmp_path, monkeypatch):
    return make_case(
        git_history, tmp_path, monkeypatch, executor="codex", reviewer="none",
        skill_libraries=[], test_command="true", max_runtime_minutes=2, stall_minutes=1,
    )


def stop_context(case, table, *, start_runs=False):
    ctx = tick_context(case, start_runs=start_runs)
    # Exercise the production killer, not tick_context's intentional no-op.
    ctx.kill_runner = processes.kill_process_group
    ctx.runner_process_exists = lambda number: any(
        row.marker == f"owner/repo#{number}" for row in table.list_processes()
    )
    ctx.baseline_launcher = lambda *args: pytest.fail("Unexpected baseline process")
    return ctx


def record_tree(case, table):
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    start = case.clock[0].timestamp()
    root = table.add(42000, cwd=worktree, start_time=start,
                     argv=("fa", "gate", "run-issue", "1"), marker="owner/repo#1")
    executor = table.add(42001, ppid=root.pid, cwd=worktree, start_time=start + 1)
    helper = table.add(42002, ppid=executor.pid, pgid=executor.pgid, sid=executor.sid,
                       cwd=worktree, start_time=start + 2)
    detached = table.add(42003, ppid=helper.pid, cwd=case.root.parent, start_time=start + 3)
    case.store.start_run(1, branch="foragent/issue-1", worktree=worktree, pid=root.pid)
    case.store.update_run(1, executor_pgid=executor.pid)
    return root, executor, helper, detached


def events(case):
    path = case.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _try_stop(call):
    # Refusal may be an exception or a no-op. Inspect every attempted signal.
    try:
        call()
    except (ValueError, PermissionError, ProcessLookupError):
        pass


@pytest.mark.parametrize("reason", ["time-limit", "stall"])
def test_gate_stop_kills_detached_executor_descendants(
    git_history, tmp_path, monkeypatch, fake_processes, reason,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    tree = record_tree(case, fake_processes)
    protected = fake_processes.host_pids | {
        fake_processes.add(43000, cwd=case.store.worktree_path(1), start_time=0).pid,
    }
    minutes = case.config.max_runtime_minutes + 11 if reason == "time-limit" else case.config.stall_minutes + 1
    case.clock[0] += timedelta(minutes=minutes)
    report = scheduler.tick(stop_context(case, fake_processes))
    run = case.store.get_run(1)
    assert protected <= fake_processes.live, "Gate stop touched an unowned process"
    assert run.status == "finished", report.actions
    assert run.outcome == ("killed" if reason == "time-limit" else "stalled-restart"), run
    assert tree[-1].pid not in fake_processes.live, (
        f"{reason}: detached grandchild survived gate stop (left executor process group/session and cwd)"
    )
    assert not {row.pid for row in tree} & fake_processes.live, "A recorded run descendant survived"


def _inactive_and_running(case, table, *, evidence):
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    other = case.store.worktree_path(2)
    case.git.add_worktree(other, "foragent/issue-2", "origin/main")
    never = case.store.worktree_path(3)
    case.git.add_worktree(never, "foragent/issue-3", "origin/main")
    outside = case.root.parent
    start = case.clock[0].timestamp()
    cwd = worktree / "helpers" if evidence == "cwd" else outside
    argv = ("worker",) if evidence == "cwd" else ("python", str(worktree / "worker.py"))
    root = table.add(42000, cwd=cwd, argv=argv, start_time=start, marker="owner/repo#1")
    child = table.add(42001, ppid=root.pid, cwd=cwd, argv=argv, start_time=start + 1)
    case.store.start_run(1, branch="foragent/issue-1", worktree=worktree, pid=root.pid)
    case.store.finish_run(1, "killed")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.close_issue(1)
    case.tracker.add_issue(2, "Still working", labels=["running"])
    active = table.add(43000, cwd=other, start_time=start, marker="owner/repo#2")
    case.store.start_run(2, branch="foragent/issue-2", worktree=other, pid=active.pid)
    case.tracker.add_issue(3, "Never launched", labels=["blocked"])
    assert case.store.get_run(3) is None
    protected = table.host_pids | {active.pid}
    for pid, directory, args in (
        (43001, other / "helpers", ("worker",)),
        (43002, outside, ("python", str(other / "worker.py"))),
        (43003, outside, ("worker",)),
        (43004, Path(str(worktree) + "-unrelated"), ("worker",)),
        # Even both cwd and argv together are not proof Foragent started it.
        (43005, worktree, ("python", str(worktree / "worker.py"))),
        (43006, never, ("python", str(never / "worker.py"))),
    ):
        protected.add(table.add(pid, cwd=directory, argv=args, start_time=start + 2).pid)
    return (root.pid, child.pid), protected


@pytest.mark.parametrize("evidence", ["cwd", "argv"])
def test_tick_reaps_inactive_worktree_processes_and_preserves_other_processes(
    git_history, tmp_path, monkeypatch, fake_processes, evidence,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    orphans, protected = _inactive_and_running(case, fake_processes, evidence=evidence)
    scheduler.tick(stop_context(case, fake_processes))
    assert protected <= fake_processes.live, "Tick touched a running issue or an unowned process"
    assert case.store.get_run(2).status == "running"
    assert not set(orphans) & fake_processes.live, (
        f"tick left an inactive issue's owned {evidence}-matched worktree process alive"
    )


def test_tick_records_issue_and_unique_reaped_process_count(git_history, tmp_path, monkeypatch, fake_processes):
    case = stop_case(git_history, tmp_path, monkeypatch)
    orphans, protected = _inactive_and_running(case, fake_processes, evidence="argv")
    # Root matches cwd AND argv; child matches argv only. Two unique processes.
    root = fake_processes.rows[orphans[0]]
    fake_processes.rows[root.pid] = replace(root, cwd=case.store.worktree_path(1))
    scheduler.tick(stop_context(case, fake_processes))
    assert protected <= fake_processes.live, "Tick touched a protected process"
    before = [event for event in events(case) if event["kind"] == "orphans-reaped"]
    reaped = [event for event in before if event["issue"] == 1]
    assert reaped, "tick emitted no kind=orphans-reaped event for issue #1"
    assert len(reaped) == 1 and (
        reaped[0].get("count") == 2 or "2" in re.findall(r"\b\d+\b", reaped[0]["summary"])
    ), f"Expected exactly two unique reaped processes: {reaped}"
    assert not set(orphans) & fake_processes.live, "Reported orphans are still alive"
    assert all(event["issue"] == 1 for event in before), before
    scheduler.tick(stop_context(case, fake_processes))
    assert protected <= fake_processes.live, "Second tick touched a protected process"
    assert [event for event in events(case) if event["kind"] == "orphans-reaped"] == before, (
        "A second tick reported the same exits again"
    )


@pytest.mark.parametrize("target", FakeProcessTable.unsafe_targets)
@pytest.mark.parametrize("recorded_as", ["runner", "executor"])
def test_stop_refuses_pid_one_and_own_lineage(
    git_history, tmp_path, monkeypatch, fake_processes, target, recorded_as,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    pid = fake_processes.unsafe_targets[target]
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1),
                         pid=pid if recorded_as == "runner" else None)
    if recorded_as == "executor":
        case.store.update_run(1, executor_pgid=pid)
    _try_stop(lambda: scheduler._kill_run(stop_context(case, fake_processes), 1, scheduler.TickReport()))
    assert not fake_processes.signals, (
        f"Unsafe {recorded_as} {target} received a signal attempt: {fake_processes.signals}"
    )
    assert fake_processes.host_pids <= fake_processes.live


@pytest.mark.parametrize("identity", ["runner", "executor"])
def test_stop_preserves_reused_process_identity(git_history, tmp_path, monkeypatch, fake_processes, identity):
    case = stop_case(git_history, tmp_path, monkeypatch)
    tree = record_tree(case, fake_processes)
    original = tree[0 if identity == "runner" else 1]
    # A bare PID/PPID traversal would kill this replacement and its new child.
    fake_processes.exit(original.pid)
    fake_processes.add(original.pid, cwd=case.store.worktree_path(1),
                       start_time=original.start_time + 100, argv=("unrelated-app",))
    stranger = fake_processes.add(44000, ppid=original.pid, pgid=original.pid, sid=original.pid,
                                  cwd=case.store.worktree_path(1), start_time=original.start_time + 101)
    _try_stop(lambda: scheduler._kill_run(stop_context(case, fake_processes), 1, scheduler.TickReport()))
    assert {original.pid, stranger.pid} <= fake_processes.live, (
        f"Reused {identity} PID was mistaken for the recorded process (start time changed)"
    )
    assert not any(target in {original.pid, stranger.pid} for _, target, _ in fake_processes.signals), (
        "A reused identity must not receive even an attempted signal"
    )


@pytest.mark.parametrize("leader_exit", ["before-stop", "after-term"])
def test_stop_never_signals_group_after_leader_exits(tmp_path, fake_processes, leader_exit):
    table = fake_processes
    leader = table.add(42000, cwd=tmp_path, start_time=10)

    def replace_group(_call=None):
        table.after_signal = None
        table.add(44000, pgid=leader.pid, sid=leader.pid, cwd=tmp_path, start_time=20)

    if leader_exit == "before-stop":
        table.exit(leader.pid)
        replace_group()
    else:
        table.after_signal = replace_group
    # Callers stop a group through its recorded identity (pid plus start time).
    with processes.recorded_tree([{"pid": leader.pid, "start_time": 10}]):
        _try_stop(lambda: processes.kill_process_group(leader.pid))
    assert 44000 in table.live, "A group number outlived its leader and killed an unrelated process"
    assert not table.leaderless_group_calls, "killpg used a numeric group after its leader was gone"


def test_stop_without_recorded_identity_signals_nothing(tmp_path, fake_processes):
    table = fake_processes
    leader = table.add(42000, cwd=tmp_path, start_time=10)
    _try_stop(lambda: processes.kill_process_group(leader.pid))
    assert not table.signals, f"A pid without a recorded identity was signaled: {table.signals}"
    assert leader.pid in table.live


def test_stop_treats_permission_denied_group_probe_as_not_ours(tmp_path, fake_processes):
    table = fake_processes
    leader = table.add(42000, cwd=tmp_path, start_time=10)
    table.denied_groups.add(leader.pid)
    _try_stop(lambda: processes.kill_process_group(leader.pid))
    assert not table.signals, f"Permission-denied group was signaled instead of refused: {table.signals}"
    assert leader.pid in table.live

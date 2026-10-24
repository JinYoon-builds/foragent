"""Kill safety regressions use only immutable fake identities and fake signals."""

from dataclasses import replace
import signal
from types import SimpleNamespace

import pytest

from foragent import processes
from foragent.state import StateStore


def owned_stop(pid, identities):
    with processes.recorded_tree(identities):
        return processes.kill_process_group(pid)


def test_empty_recorded_ownership_does_not_authorize_a_signal(fake_process_table, tmp_path):
    table = fake_process_table
    table.add(42000, cwd=tmp_path, start_time=10)
    with processes.recorded_tree([]), pytest.raises(ValueError, match="No recorded process identity"):
        processes.kill_process_group(42000)
    assert not table.calls


@pytest.mark.parametrize("present", [False, True])
def test_stop_without_recorded_tree_refuses_before_process_io(fake_process_table, tmp_path, monkeypatch, present):
    table = fake_process_table
    if present:
        table.add(42000, cwd=tmp_path, start_time=10)
    before = table.live
    monkeypatch.setattr(processes, "list_processes", lambda: pytest.fail("Unrecorded stop must not inspect PIDs"))
    with pytest.raises(ValueError, match="No recorded process identity"):
        processes.kill_process_group(42000)
    assert table.live == before
    assert not table.calls


@pytest.mark.parametrize("when", ["before-stop", "after-term"])
def test_recorded_stop_never_reuses_an_exited_leaders_group(fake_process_table, tmp_path, when):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    identities = processes.observe_tree(roots=(root.pid,))

    def replace_group(call=None):
        table.after_signal = None
        table.add(44000, pgid=root.pid, sid=root.pid, cwd=tmp_path, start_time=20)

    if when == "before-stop":
        table.exit(root.pid)
        replace_group()
    else:
        table.after_signal = replace_group
    owned_stop(root.pid, identities)
    assert 44000 in table.live and child.pid not in table.live
    assert not table.leaderless_group_calls
    assert all(kind == "pid" and sig != 0 for kind, _, sig in table.calls)


def test_missing_leader_never_requires_a_group_probe(fake_process_table, tmp_path, monkeypatch):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    identities = processes.observe_tree(roots=(root.pid,))
    table.exit(root.pid)
    monkeypatch.setattr(processes.os, "killpg", lambda *args: pytest.fail("No group I/O is allowed"))
    owned_stop(root.pid, identities)
    assert child.pid not in table.live


def test_permission_denied_probe_is_never_retried_as_signal(fake_process_table, tmp_path, monkeypatch):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))
    calls = []

    def denied(pid, sig):
        calls.append((pid, sig))
        raise PermissionError(pid)

    monkeypatch.setattr(processes.os, "kill", denied)
    with pytest.raises(PermissionError):
        owned_stop(root.pid, identities)
    assert calls == [(root.pid, signal.SIGTERM)]
    assert not table.signals and root.pid in table.live


@pytest.mark.parametrize("field,value", [("start_time", 20), ("ppid", 44000), ("pgid", 44000), ("sid", 44000)])
def test_reuse_between_pid_probe_and_signal_is_refused(fake_process_table, tmp_path, monkeypatch, field, value):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))
    scans = 0

    def replace_after_candidate_scan():
        nonlocal scans
        rows = table.list_processes()
        scans += 1
        # Two ownership scans, two expansion scans, then the signal candidate.
        if scans == 5:
            table.rows[root.pid] = replace(root, **{field: value})
        return rows

    monkeypatch.setattr(processes, "list_processes", replace_after_candidate_scan)
    if field == "start_time":
        owned_stop(root.pid, identities)
    else:
        with pytest.raises(PermissionError, match="identity changed"):
            owned_stop(root.pid, identities)
    assert scans >= 6
    assert root.pid in table.live and not table.calls


def test_reused_pid_is_not_reanchored_by_same_record_update(fake_process_table, tmp_path):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    run = store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    table.exit(root.pid)
    table.add(root.pid, cwd=tmp_path, start_time=20)
    updated = store.update_run(1, pid=root.pid)
    assert updated.process_identities == run.process_identities
    owned_stop(root.pid, updated.process_identities)
    assert root.pid in table.live and not table.signals


def test_observed_detached_child_survives_reload_as_owned(fake_process_table, tmp_path):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path.parent, start_time=11)
    store.update_run(1, phase="implement")
    table.exit(root.pid)
    table.rows[child.pid] = replace(child, ppid=1)
    reloaded = StateStore(store.root).get_run(1)
    assert owned_stop(root.pid, reloaded.process_identities) == 1
    assert child.pid not in table.live


def test_enumeration_uses_fresh_kernel_start_time_and_excludes_denied_or_zombie(monkeypatch, no_real_signals):
    # Obtain the production function, hidden by the fail-closed unit fixture.
    import importlib.util

    spec = importlib.util.spec_from_file_location("foragent._enumeration_test", processes.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__("sys").modules, spec.name, module)
    spec.loader.exec_module(module)
    psutil = module.psutil

    class Process:
        def __init__(self, pid, start=10, status="running", denied=False):
            self.pid, self.start, self.state, self.denied = pid, start, status, denied

        def create_time(self):
            if self.denied:
                raise psutil.AccessDenied(self.pid)
            return self.start

        def ppid(self):
            return 1

        def status(self):
            return self.state

    monkeypatch.setattr(psutil, "process_iter", lambda: iter([
        Process(42000), Process(42001), Process(42002, status=psutil.STATUS_ZOMBIE),
        Process(42003, denied=True),
    ]))
    monkeypatch.setattr(psutil, "Process", lambda pid: Process(pid, start=20 if pid == 42001 else 10))
    monkeypatch.setattr(module, "os", SimpleNamespace(getpgid=lambda pid: pid, getsid=lambda pid: pid))
    rows = module.list_processes()
    assert [(row.pid, row.start_time) for row in rows] == [(42000, 10)]
    assert module.read_process_identity(42000) == module.ProcessIdentity(42000, 1, 42000, 42000, 10)
    changing = iter((Process(42000), Process(42000, start=20)))
    monkeypatch.setattr(psutil, "Process", lambda pid: next(changing))
    assert module.read_process_identity(42000) is None
    for process in (Process(42000, status=psutil.STATUS_ZOMBIE), Process(42000, denied=True)):
        monkeypatch.setattr(psutil, "Process", lambda pid: process)
        assert module.read_process_identity(42000) is None

    def missing_process(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing_process)
    assert module.read_process_identity(42000) is None

    def denied_table():
        raise PermissionError("process table denied")

    monkeypatch.setattr(psutil, "process_iter", denied_table)
    with pytest.raises(PermissionError, match="ownership cannot be verified"):
        module.list_processes()


def test_parent_reuse_during_enumeration_does_not_authorize_new_children(fake_process_table, tmp_path, monkeypatch):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=21)
    first = table.list_processes()
    table.add(root.pid, cwd=tmp_path, start_time=20)
    second = table.list_processes()
    snapshots = iter((first, second))
    monkeypatch.setattr(processes, "list_processes", lambda: next(snapshots))
    assert processes.observe_tree(identities) == identities
    assert child.pid not in {item["pid"] for item in identities}


def test_logged_executor_persists_detached_child_after_leader_exit(fake_process_table, tmp_path, monkeypatch):
    from process_fakes import install_popen

    table = fake_process_table
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path)
    install_popen(monkeypatch, table, matches=lambda argv: argv == ["offline-executor"], children=(42001,))
    with processes.track_process_group(
        lambda pid: store.update_run(1, executor_pgid=pid),
        tree_callback=lambda identities: store.update_run(1, process_identities=identities),
    ):
        assert processes.run_logged(["offline-executor"], cwd=tmp_path, timeout_s=1,
                                    log_path=tmp_path / "run.log")[:2] == (0, False)
    run = StateStore(store.root).get_run(1)
    assert run.executor_pgid is None
    assert 42000 not in table.live and 42001 in table.live
    assert owned_stop(42000, run.process_identities) == 1
    assert 42001 not in table.live


def test_tick_reaps_inactive_tree_even_during_github_pause(fake_process_table, tmp_path):
    from datetime import UTC, datetime, timedelta
    from foragent import scheduler
    from foragent.config import Config
    from foragent.tracker import FakeTracker

    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    now = datetime(2026, 10, 24, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    store.finish_run(1, "killed")
    store.pause_github(now + timedelta(minutes=10))
    ctx = scheduler.TickContext(
        repo="owner/repo", repo_root=tmp_path, store=store, tracker=FakeTracker(),
        config=Config(), now=lambda: now, launcher=lambda number: pytest.fail("Unexpected launch"),
        git=SimpleNamespace(fetch=lambda *args: pytest.fail("Unexpected fetch")),
    )
    report = scheduler.tick(ctx)
    assert "GitHub rate limit" in report.skipped
    assert root.pid not in table.live
    assert "orphans-reaped" in (store.root / "orch-events.jsonl").read_text()


def test_runner_can_record_its_own_birth_for_a_later_gate(fake_process_table, tmp_path, monkeypatch):
    table = fake_process_table
    root = table.add(table.self_pid, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    run = store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    assert run.process_identities == [{"pid": root.pid, "start_time": root.start_time}]
    assert owned_stop(root.pid, run.process_identities) == 0
    assert not table.signals
    # Another gate has its own independent lineage/group/session.
    monkeypatch.setattr(processes.os, "getpid", lambda: 92003)
    monkeypatch.setattr(processes.os, "getppid", lambda: 92002)
    monkeypatch.setattr(processes.os, "getpgrp", lambda: 92000)
    monkeypatch.setattr(processes.os, "getsid", lambda pid: 91999)
    assert owned_stop(root.pid, run.process_identities) == 1
    assert root.pid not in table.live


def test_explicit_new_executor_birth_can_reuse_a_previous_executors_pid(fake_process_table, tmp_path):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    old = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    store.update_run(1, executor_pgid=old.pid)
    table.exit(old.pid)
    store.update_run(1, executor_pgid=None)
    new = table.add(old.pid, ppid=root.pid, cwd=tmp_path, start_time=20)
    run = store.update_run(1, executor_pgid=new.pid,
                           process_identities=processes.observe_tree(roots=(new.pid,)))
    assert {item["pid"]: item["start_time"] for item in run.process_identities}[new.pid] == new.start_time
    assert owned_stop(root.pid, run.process_identities) == 2
    assert not {root.pid, new.pid} & table.live


def test_logged_observer_scans_once_per_second_and_keeps_reparented_children(fake_process_table, tmp_path, monkeypatch):
    from process_fakes import _ModuleView, install_popen

    table = fake_process_table
    observations, recorded = [], []
    observe = processes.observe_tree
    clock = SimpleNamespace(now=0.0, target=None, process=None, waits=[])

    def observe_at_clock(*args, **kwargs):
        observations.append(clock.now)
        return observe(*args, **kwargs)

    class Event:
        stopped = False

        def set(self):
            self.stopped = True

        def wait(self, timeout):
            if self.stopped:
                return True
            clock.waits.append(timeout)
            if clock.now + timeout > 3.25:
                clock.now = 3.25
                self.set()
                return True
            clock.now += timeout
            if clock.now == 1:
                table.add(42001, ppid=42000, cwd=tmp_path, start_time=11)
                table.add(42002, ppid=42001, cwd=tmp_path.parent, start_time=12)
            elif clock.now == 2:
                table.exit(42001)  # The known grandchild is now reparented to init.
            return False

    class Thread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            clock.target = self.target

        def join(self):
            pass

    def communicate(process):
        clock.target()  # Drive the observer with virtual time, without a real thread.

    install_popen(monkeypatch, table, matches=lambda argv: argv == ["offline-executor"], communicate=communicate)
    monkeypatch.setattr(processes, "threading", _ModuleView(processes.threading, Thread=Thread, Event=Event))
    monkeypatch.setattr(processes, "observe_tree", observe_at_clock)
    with processes.track_process_group(lambda pid: None, tree_callback=lambda identities: recorded.append(identities)):
        assert processes.run_logged(["offline-executor"], cwd=tmp_path, timeout_s=10,
                                    log_path=tmp_path / "run.log")[:2] == (0, False)
    assert observations == [0, 1, 2, 3, 3.25]  # Launch, periodic observations, final snapshot.
    assert clock.waits == [1, 1, 1, 1]
    assert table.list_calls == 2 * len(observations)
    assert {item["pid"] for item in recorded[-1]} == {42000, 42001, 42002}
    assert table.rows[42002].ppid == 1 and 42002 in table.live
    assert owned_stop(42000, recorded[-1]) == 1
    assert 42002 not in table.live


@pytest.mark.parametrize("finished", [False, True])
def test_logged_cleanup_never_infers_missing_launch_identity(fake_process_table, tmp_path, monkeypatch, finished):
    from process_fakes import install_popen

    def communicate(process):
        if not finished:
            raise RuntimeError("worker interrupted")

    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["offline-executor"],
                  communicate=communicate)
    monkeypatch.setattr(processes, "list_processes", lambda: ())
    monkeypatch.setattr(processes, "kill_process_group", lambda pid: pytest.fail("Unrecorded cleanup reached killer"))

    def run():
        return processes.run_logged(["offline-executor"], cwd=tmp_path, timeout_s=10,
                                    log_path=tmp_path / "run.log", terminate_children=True)

    if finished:
        assert run()[:2] == (0, False)
    else:
        with pytest.raises(PermissionError, match="Cannot verify executor identity"):
            run()
    assert not fake_process_table.calls


@pytest.mark.parametrize("field", ["pid", "executor_pgid"])
@pytest.mark.parametrize("repeat", [False, True])
def test_numeric_update_without_birth_evidence_is_not_adopted(fake_process_table, tmp_path, field, repeat):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path)
    run = store.update_run(1, **{field: root.pid})
    if repeat:
        table.add(root.pid, cwd=tmp_path, start_time=20)
        run = store.update_run(1, **{field: root.pid})
    assert run.process_identities == []
    with pytest.raises(ValueError, match="No recorded process identity"):
        owned_stop(root.pid, run.process_identities)
    assert root.pid in table.live and not table.calls


def test_numeric_executor_update_does_not_adopt_an_unrelated_root(fake_process_table, tmp_path):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    stranger = table.add(43000, cwd=tmp_path, start_time=11)
    store = StateStore(tmp_path / "state")
    run = store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    updated = store.update_run(1, executor_pgid=stranger.pid)
    assert updated.process_identities == run.process_identities
    assert owned_stop(root.pid, updated.process_identities) == 1
    assert stranger.pid in table.live
    assert table.calls == [("pid", root.pid, signal.SIGTERM)]


@pytest.mark.parametrize("phase", ["term", "kill", "missing", "reused", "protected", "unrecorded"])
def test_all_stop_paths_use_no_group_calls_or_pid_probes(fake_process_table, tmp_path, monkeypatch, phase):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))
    if phase == "kill":
        table.ignore_term.add(root.pid)
    elif phase == "missing":
        table.exit(root.pid)
    elif phase == "reused":
        table.add(root.pid, cwd=tmp_path, start_time=20)
    elif phase == "protected":
        table.rows[root.pid] = replace(root, pgid=table.own_pgid)
    elif phase == "unrecorded":
        identities = []

    def no_group(*args):
        pytest.fail(f"Group I/O is forbidden: {args}")

    def signal_only(pid, signum):
        assert signum != 0, "No numeric PID probe is allowed"
        table.kill(pid, signum)

    monkeypatch.setattr(processes.os, "killpg", no_group)
    monkeypatch.setattr(processes.os, "kill", signal_only)
    if phase == "unrecorded":
        with pytest.raises(ValueError, match="No recorded process identity"):
            owned_stop(root.pid, identities)
    else:
        owned_stop(root.pid, identities)
    assert all(kind == "pid" and signum != 0 for kind, _, signum in table.calls)
    assert [sig for _, _, sig in table.calls] == (
        [signal.SIGTERM, signal.SIGKILL] if phase == "kill" else [signal.SIGTERM] if phase == "term" else []
    )


@pytest.mark.parametrize("race", ["none", "before-open", "after-verification", "denied", "escalation"])
def test_pidfd_signals_are_lifetime_bound_and_descriptors_close(fake_process_table, tmp_path, monkeypatch, race):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))
    if race == "escalation":
        table.ignore_term.add(root.pid)
    handles, closed, sent = {}, [], []

    def open_fd(pid, flags):
        assert flags == 0
        if race == "before-open":
            table.add(pid, cwd=tmp_path, start_time=20)
        fd = 100 + len(handles)
        handles[fd] = table.rows[pid]
        return fd

    def send_fd(fd, signum):
        identity = handles[fd]
        sent.append((fd, signum))
        if race == "denied":
            raise PermissionError("not ours")
        if race == "after-verification":
            table.exit(identity.pid)
            table.add(identity.pid, cwd=tmp_path, start_time=20)
        # Model the kernel's lifetime binding, never a lookup by PID alone.
        if table.rows[identity.pid].start_time != identity.start_time:
            raise ProcessLookupError(identity.pid)
        table.kill(identity.pid, signum)

    monkeypatch.setattr(processes.os, "pidfd_open", open_fd)
    monkeypatch.setattr(processes.signal, "pidfd_send_signal", send_fd)
    monkeypatch.setattr(processes.os, "close", closed.append)
    monkeypatch.setattr(processes.os, "kill", lambda *args: pytest.fail("pidfd path used a numeric signal"))
    if race == "denied":
        with pytest.raises(PermissionError, match="not ours"):
            owned_stop(root.pid, identities)
    else:
        assert owned_stop(root.pid, identities) == (1 if race in {"none", "escalation"} else 0)
    assert closed == list(handles)
    assert [sig for _, sig in sent] == (
        [] if race == "before-open" else [signal.SIGTERM, signal.SIGKILL] if race == "escalation" else [signal.SIGTERM]
    )
    assert (root.pid in table.live) == (race not in {"none", "escalation"})
    if race in {"before-open", "after-verification", "denied"}:
        assert not table.calls


def test_pidfd_unavailable_in_kernel_rechecks_before_numeric_signal(fake_process_table, tmp_path, monkeypatch):
    import errno

    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    identities = processes.observe_tree(roots=(root.pid,))

    def unavailable(pid, flags):
        raise OSError(errno.ENOSYS, "pidfd unavailable")

    monkeypatch.setattr(processes.os, "pidfd_open", unavailable)
    assert owned_stop(root.pid, identities) == 1
    assert table.calls == [("pid", root.pid, signal.SIGTERM)]

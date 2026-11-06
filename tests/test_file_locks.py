"""Lock deadlines use fake clocks/processes; concurrency uses only temporary files and threads."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import fcntl
import json
import os
from threading import Event
from types import SimpleNamespace

import pytest

from foragent import capacity, file_locks, merge_owner, orch_events, orch_facts, orch_tick_outcome
from foragent import order_flush_journal, order_prepare_owner, scheduler
from foragent import watch_open
from foragent.config import Config
from foragent.git import GitRepo
from foragent.state import StateStore


@pytest.fixture(autouse=True)
def fake_lsof(monkeypatch):
    def run(command, **kwargs):
        assert command[0] == "lsof", f"Unexpected external command: {command}"
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(file_locks.subprocess, "run", run)


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(file_locks.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(file_locks.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(file_locks, "POLL_SECONDS", 60)
    return now


def busy(descriptor, operation):
    if operation != fcntl.LOCK_UN:
        assert operation & fcntl.LOCK_NB, "a blocking syscall has no deadline"
        raise BlockingIOError


@pytest.mark.parametrize("mode", [fcntl.LOCK_EX, fcntl.LOCK_SH])
def test_default_deadline_records_holder_without_waiting_for_state(tmp_path, monkeypatch, clock, mode):
    store = StateStore(tmp_path / "repo")
    store.root.mkdir()
    path = store.root / "state.lock"
    holder = {"pid": 64264, "command": ["foragent.merge_owner"], "start_time": None}
    path.write_text(json.dumps(holder))
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    with path.open("r") as stream, pytest.raises(file_locks.LockTimeout, match="600s"):
        with file_locks.locked(stream.fileno(), path, mode=mode, journal_root=store.root):
            pytest.fail("a held lock was entered")
    assert clock[0] == 600
    [event] = orch_events.events(store, "lock-timeout")
    assert event["holders"] == [holder]
    assert event["lock"] == str(path) and event["waiter"]["pid"] == os.getpid()
    assert event["wait_seconds"] == 600


@pytest.mark.parametrize("kind", ["state", "machine", "capacity-file", "merge", "main-write", "notify", "worktree",
                                  "baseline", "route", "claim", "flush", "openers"])
def test_control_locks_share_the_configured_deadline(tmp_path, monkeypatch, clock, kind):
    store = StateStore(tmp_path / "repo")
    store.root.mkdir()
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    guards = {
        "state": store._state_lock,
        "machine": lambda: capacity.machine_lock(store),
        "capacity-file": lambda: capacity._flock(store.root / "budget-history.lock"),
        "merge": store.merge_lock,
        "main-write": GitRepo(tmp_path, run=lambda args: str(store.root)).publish_lock,
        "notify": store.notify_lock,
        "worktree": lambda: store.worktree_lock(1, timeout=50),
        "route": lambda: order_prepare_owner.publication_gate(store.root / "prepare"),
        "flush": lambda: order_flush_journal.locked(store),
        "openers": lambda: watch_open._opening(store),
    }
    if kind == "baseline":
        with pytest.raises(file_locks.LockTimeout):
            store.lock_acceptance_baseline("merge-owner")
    elif kind == "claim":
        record = {}
        assert not order_prepare_owner.claim(store.root / "prepare", record, ["a"],
                                             lambda: pytest.fail("unlocked claim"))
        assert not record
    else:
        with pytest.raises(file_locks.LockTimeout):
            with guards[kind]():
                pytest.fail("a held lock was entered")
    assert clock[0] == 2
    assert orch_events.events(store, "lock-timeout")[-1]["wait_seconds"] == 2


@pytest.mark.parametrize("mode", [fcntl.LOCK_EX, fcntl.LOCK_SH])
def test_uncontended_lock_records_owner_without_process_probes(tmp_path, monkeypatch, mode):
    path = tmp_path / "read-only-facts.lock"

    def forbidden(*args, **kwargs):
        pytest.fail("recording lock ownership must not probe a process or run a command")

    monkeypatch.setattr(file_locks.psutil, "Process", forbidden)
    monkeypatch.setattr(file_locks.subprocess, "run", forbidden)
    with path.open("a") as stream, file_locks.locked(stream.fileno(), path, mode=mode):
        source = (file_locks.owner_path(path) if mode == fcntl.LOCK_EX
                  else next(tmp_path.glob(f".{path.name}.reader-*")))
        holder = json.loads(source.read_text())
        assert holder["pid"] == os.getpid()
        assert holder["command"] and holder["acquired_at"]
    assert not list(tmp_path.glob(f".{path.name}.reader-*"))


def test_shared_lock_holders_are_recorded_and_removed_on_unwind(tmp_path, monkeypatch, clock):
    path = tmp_path / "shared.lock"
    path.touch()
    monkeypatch.setattr(file_locks.fcntl, "flock", lambda *args: None)
    with path.open("r") as stream:
        with pytest.raises(ValueError, match="unwind"):
            with file_locks.locked(stream.fileno(), path, mode=fcntl.LOCK_SH):
                [holder] = file_locks.holders(path)
                assert holder["pid"] == os.getpid()
                monkeypatch.setattr(file_locks.fcntl, "flock", busy)
                with path.open("a") as writer, pytest.raises(file_locks.LockTimeout):
                    file_locks.acquire(writer.fileno(), path, timeout=1)
                [event] = orch_events.events(StateStore(tmp_path), "lock-timeout")
                assert event["holders"] == [holder]
                raise ValueError("unwind")
    assert file_locks.holders(path) == []
    assert not list(path.parent.glob(f".{path.name}.reader-*"))


@pytest.mark.parametrize("outer", ["machine", "merge", "route"])
def test_state_then_machine_is_rejected_before_waiting(tmp_path, outer):
    store = StateStore(tmp_path / "repo")
    guards = {"machine": lambda: capacity.machine_lock(store), "merge": store.merge_lock,
              "route": lambda: order_prepare_owner.publication_gate(store.root / "prepare")}
    with store._state_lock():
        with pytest.raises(RuntimeError, match="Lock order inversion"):
            with guards[outer]():
                pytest.fail("inverted locks were entered")
    with capacity.machine_lock(store), store._state_lock():
        pass


@pytest.mark.parametrize("names", [("owner__repo", "owner__repo-sandbox"), ("owner__repo", "owner__repo.b"),
                                   ("owner__repo", "owner__repo_b")])
def test_sibling_state_locks_in_lock_order_pass_the_guard(tmp_path, names):
    from contextlib import ExitStack
    paths = [tmp_path / name / "state.lock" for name in names]
    for path in paths:
        path.parent.mkdir()
        path.touch()

    def take(order):
        with ExitStack() as stack:
            for path in order:
                descriptor = os.open(path, os.O_RDWR)
                stack.callback(os.close, descriptor)
                stack.enter_context(file_locks.locked(descriptor, path, mode=fcntl.LOCK_SH))

    take(sorted(paths, key=file_locks.lock_order))
    with pytest.raises(RuntimeError, match="Lock order inversion"):
        take(sorted(paths, key=file_locks.lock_order, reverse=True))


def test_legacy_holder_probe_distinguishes_holders_from_waiters(tmp_path, monkeypatch):
    path = tmp_path / "state.lock"
    path.touch()
    calls = []

    def lsof(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="p2158\ncfa\nf4\nlR\np30394\ncfa\nf5\n")

    monkeypatch.setattr(file_locks.subprocess, "run", lsof)
    assert file_locks.holders(path) == [{"pid": 2158, "command": "fa"}]
    assert calls == [["lsof", "-nP", "-Fpcfl", "--", str(path)]]


@pytest.mark.parametrize("identity", [
    {"start_time": 1},
    {"acquired_at": datetime.fromtimestamp(1, UTC).isoformat()},
])
def test_holder_metadata_from_a_reused_pid_is_not_reported(tmp_path, monkeypatch, identity):
    path = tmp_path / "state.lock"
    path.write_text(json.dumps({"pid": 2158, "command": ["old run"], **identity}))
    monkeypatch.setattr(file_locks.psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: 2))
    monkeypatch.setattr(file_locks.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout="p64264\ncforagent.merge_owner\nf3\nlW\n"))
    assert file_locks.holders(path) == [{"pid": 64264, "command": "foragent.merge_owner"}]


def test_immediate_owner_probe_never_waits_or_probes_holders(tmp_path, monkeypatch, clock):
    store = StateStore(tmp_path / "repo")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    monkeypatch.setattr(file_locks, "holders", lambda path: pytest.fail("an immediate probe must defer quietly"))
    assert store.lock_acceptance_baseline("merge-owner", blocking=False) is None
    assert clock[0] == 0
    assert not orch_events.events(store, "lock-timeout")


def test_zero_configured_limit_still_records_a_blocked_wait(tmp_path, monkeypatch, clock):
    store = StateStore(tmp_path / "repo")
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    with pytest.raises(file_locks.LockTimeout):
        with store._state_lock():
            pytest.fail("the state lock was never free")
    [event] = orch_events.events(store, "lock-timeout")
    assert event["wait_seconds"] == clock[0] == 0


@pytest.mark.parametrize("kind", ["run", "workload"])
def test_repeated_machine_lock_probes_obey_lock_limit(tmp_path, monkeypatch, clock, kind):
    store = StateStore(tmp_path / "repo")
    store.root.mkdir()
    path = store.root.parent / capacity.LOCK_FILE
    holder = {"pid": 64264, "command": ["foragent.merge_owner"]}
    path.write_text(json.dumps(holder))
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    monkeypatch.setattr(capacity, "_sensor_block", lambda: None)
    monkeypatch.setattr(capacity, "_machine_scan", lambda *args, **kwargs: ([], False))
    monkeypatch.setattr(capacity, "workload_budget", lambda store: 1)
    with pytest.raises(file_locks.LockTimeout):
        if kind == "run":
            capacity.admit_run(store, 1, {}, deadline=100, now=lambda: datetime.now(UTC), waiting=lambda: None)
        else:
            with capacity.hold_workload(store, Config(repo="o/r"), wait_s=100):
                pytest.fail("the machine lock was never free")
    assert clock[0] == 2
    [event] = orch_events.events(store, "lock-timeout")
    assert event["holders"] == [holder] and event["wait_seconds"] == 2


def test_repeated_test_slot_guard_probes_obey_lock_limit(tmp_path, monkeypatch, clock):
    store = StateStore(tmp_path / "repo")
    machine = tmp_path / "machine"
    machine.mkdir()
    path = machine / capacity.TEST_SLOTS_LOCK
    holder = {"pid": 64264, "command": ["fa", "test"]}
    path.write_text(json.dumps(holder))
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    with file_locks.reporting(store.root), pytest.raises(file_locks.LockTimeout):
        with capacity.hold_test_slot(1, deadline=100):
            pytest.fail("the test slot guard was never free")
    assert clock[0] == 2
    [event] = orch_events.events(store, "lock-timeout")
    assert event["holders"] == [holder] and event["wait_seconds"] == 2


def test_inherited_owner_descriptor_keeps_lock_and_adopts_child_identity(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "repo")
    parent = store.lock_acceptance_baseline("merge-owner", blocking=False)
    inherited = os.dup(parent.fileno())
    parent.close()
    assert store.lock_acceptance_baseline("merge-owner", blocking=False) is None
    child = {"pid": 64264, "command": ["foragent.merge_owner"]}
    monkeypatch.setattr(file_locks, "owner", lambda: child)
    monkeypatch.setattr(merge_owner, "compact_queue", lambda *args: None)
    monkeypatch.setattr(merge_owner, "reap_checks", lambda *args: None)
    monkeypatch.setattr("foragent.scheduler_config.fetch_base", lambda *args: None)
    observed = []
    monkeypatch.setattr(merge_owner, "tick", lambda *args: observed.extend(
        file_locks.holders(store.root / "merge-owner.lock")))
    merge_owner.run_process({"repo_root": str(tmp_path), "repo": "o/r", "store_root": str(store.root),
                             "config": {"merge_owner": True}, "committed_config": {}}, inherited)
    assert observed == [child]
    successor = store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert successor is not None
    successor.close()


def test_timeout_unwinds_tick_and_next_tick_completes(tmp_path, monkeypatch, clock):
    store = StateStore(tmp_path / "repo")
    store.root.mkdir()
    state = store.root / "state.lock"
    state.write_text(json.dumps({"pid": 2158, "command": ["fa", "run-issue", "2158"]}))
    inode = state.stat().st_ino
    contended = [True]

    def flock(descriptor, operation):
        if contended[0] and os.fstat(descriptor).st_ino == inode:
            busy(descriptor, operation)

    monkeypatch.setattr(file_locks.fcntl, "flock", flock)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "1")
    config = Config(repo="o/r", tick_failure_alert=1)

    def tick():
        with store.tick_lock() as acquired:
            assert acquired
            with store._state_lock():
                return "finished"

    with pytest.raises(file_locks.LockTimeout):
        orch_tick_outcome.run_recorded(store, config, "o/r", tick)
    assert orch_tick_outcome.read(store)["status"] == "failed"
    assert clock[0] == 1, "failure bookkeeping must not start another lock wait"
    assert len(orch_events.events(store, "lock-timeout")) == 1
    contended[0] = False
    assert orch_tick_outcome.run_recorded(store, config, "o/r", tick) == "finished"
    assert orch_tick_outcome.read(store)["consecutive_failures"] == 0
    assert file_locks._HELD.get() == ()


def test_cli_timeout_does_not_wait_again_while_unwinding(tmp_path, monkeypatch, clock, capsys):
    from foragent import cli

    store = StateStore(tmp_path / "repo")
    store.root.mkdir()
    state = store.root / "state.lock"
    state.touch()
    inode = state.stat().st_ino

    def flock(descriptor, operation):
        if os.fstat(descriptor).st_ino == inode:
            busy(descriptor, operation)

    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(file_locks.fcntl, "flock", flock)
    monkeypatch.setattr(cli, "_parser", lambda: SimpleNamespace(
        parse_args=lambda argv: SimpleNamespace(command="gate")))
    monkeypatch.setattr(cli, "_launched_refusal", lambda args: None)
    monkeypatch.setattr(cli, "_free_text_from_stdin", lambda args: None)
    monkeypatch.setattr(cli, "_leave_background", lambda: None)

    def work():
        try:
            with store._state_lock():
                pass
        finally:
            with store._state_lock():
                pass

    def command(args):
        orch_tick_outcome.run_recorded(store, Config(repo="o/r"), "o/r", work)
        return 0

    monkeypatch.setattr(cli.commands, "module", lambda name: SimpleNamespace(run=command))
    assert cli.main(["gate", "tick"]) == 1
    assert clock[0] == 1 and "Timed out" in capsys.readouterr().err
    assert len(orch_events.events(store, "lock-timeout")) == 1
    monkeypatch.setattr(file_locks.fcntl, "flock", lambda *args: None)
    assert cli.main(["gate", "tick"]) == 0


def test_owner_pass_timeout_unwinds_without_waiting_again(tmp_path, monkeypatch, clock):
    store = StateStore(tmp_path / "repo")
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, store=store, config=Config(merge_owner=True),
                          tracker=SimpleNamespace(), git=SimpleNamespace(), now=lambda: datetime.now(UTC))
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(file_locks.fcntl, "flock", busy)
    monkeypatch.setattr(merge_owner, "reap_checks", lambda *args: None)

    def work(ctx, report):
        try:
            with ctx.store._state_lock():
                pass
        finally:
            with ctx.store._state_lock():
                pass

    monkeypatch.setattr(merge_owner, "_tick", work)
    with pytest.raises(file_locks.LockTimeout):
        merge_owner.tick(ctx, scheduler.TickReport())
    assert clock[0] == 1 and len(orch_events.events(store, "lock-timeout")) == 1
    monkeypatch.setattr(file_locks.fcntl, "flock", lambda *args: None)
    merge_owner.tick(ctx, scheduler.TickReport())


def test_gate_snapshot_and_merge_owner_pass_finish_together(tmp_path, monkeypatch):
    """Recreate the conflicting sections of a gate tick and an owner pass with fake work."""
    store = StateStore(tmp_path / "data" / "o__r")
    store.root.mkdir(parents=True)
    (store.root / "state.lock").touch()
    (store.root.parent / capacity.LOCK_FILE).touch()
    owner_has_machine, snapshot_started = Event(), Event()
    first_shared, attempts = [], []
    original = orch_facts._lock_shared

    def shared(stack, path):
        attempts.append(path)
        if not first_shared:
            first_shared.append(path)
        if path == store.root.parent / capacity.LOCK_FILE:
            snapshot_started.set()
        return original(stack, path)

    monkeypatch.setattr(orch_facts, "_lock_shared", shared)
    monkeypatch.setattr(merge_owner, "reap_checks", lambda *args: None)

    def owner_work(ctx, report):
        with capacity.machine_lock(store):
            owner_has_machine.set()
            assert snapshot_started.wait(3)
            with store._state_lock():
                store._write_json(store.root / "owner-completed.json", {"finished": True})

    monkeypatch.setattr(merge_owner, "_tick", owner_work)
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, store=store, config=Config(merge_owner=True),
                          tracker=SimpleNamespace(), git=SimpleNamespace(), now=lambda: datetime.now(UTC))

    def gate_work():
        assert owner_has_machine.wait(3)
        return orch_tick_outcome.run_recorded(
            store, ctx.config, "o/r", lambda: orch_facts._freeze(store, tmp_path / "snapshot"))

    # A short real deadline prevents even a regression from stranding the test executor.
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(merge_owner.tick, ctx, scheduler.TickReport())
        gate = pool.submit(gate_work)
        assert gate.result(timeout=5) is not None
        owner.result(timeout=5)
    assert first_shared == [store.root.parent / capacity.LOCK_FILE], attempts
    assert (store.root / "owner-completed.json").exists()
    assert not orch_events.events(store, "lock-timeout")


def test_locking_a_data_file_keeps_its_content(tmp_path):
    """11-06: the lane lesson notebook is locked directly; release truncated it to zero bytes."""
    from foragent.file_locks import file_lock, owner_path

    notebook = tmp_path / "MEMORY.new.md"
    notebook.write_text("- [id:a] kept lesson\n", encoding="utf-8")
    with file_lock(notebook):
        assert notebook.read_text(encoding="utf-8") == "- [id:a] kept lesson\n"
        assert owner_path(notebook).is_file()
        with notebook.open("a", encoding="utf-8") as stream:
            stream.write("- [id:b] new lesson\n")
    assert notebook.read_text(encoding="utf-8") == "- [id:a] kept lesson\n- [id:b] new lesson\n"
    assert not owner_path(notebook).exists()

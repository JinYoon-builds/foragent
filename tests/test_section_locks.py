"""Section ownership survives work without a kernel hold; exercise real contenders."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from foragent import file_locks, merge_owner, orch_events, orch_stop, order_publications_process, state_claims
from foragent.config import Config
from foragent.git import GitRepo, main_write_lock
from foragent.scheduler import TickReport
from foragent.state import StateStore

# Reuse the locked acceptance contract's guard without modifying its files.
sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_queue_only_main_locks import LockGuard  # noqa: E402


KINDS = ("merge", "publish", "worktree", "stop")


def lock_case(root, kind):
    store = StateStore(root)
    root.mkdir(parents=True, exist_ok=True)
    git = GitRepo(root, run=lambda args: str(root))
    return {
        "merge": (store.merge_lock, root / "merge.lock"),
        "publish": (lambda: main_write_lock(git), root / "foragent-publish.lock"),
        "worktree": (lambda: store.worktree_lock(1), root / "runs/issue-1.worktree.lock"),
        "stop": (lambda: orch_stop.record_lock(store), root / "stop.lock"),
        "tick": (store.hold_tick_lock, root / "tick.lock"),
    }[kind]


CHILD = """
import os, sys
from pathlib import Path
from foragent import orch_stop
from foragent.file_locks import LockTimeout
from foragent.git import GitRepo
from foragent.state import StateStore
root, kind, action = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = StateStore(root)
git = GitRepo(root, run=lambda args: str(root))
lock = {"merge": store.merge_lock, "publish": git.publish_lock,
        "worktree": lambda: store.worktree_lock(1), "stop": lambda: orch_stop.record_lock(store)}[kind]
try:
    with lock():
        if action == "die":
            os._exit(0)
        print("entered", flush=True)
except LockTimeout:
    print("excluded", flush=True)
"""


def child(root, kind, action):
    return subprocess.run([sys.executable, "-c", CHILD, str(root), kind, action],
                          capture_output=True, text=True, check=True,
                          env={**os.environ, "FORAGENT_LOCK_TIMEOUT_SECONDS": "0.05"})


@pytest.mark.parametrize("kind", KINDS)
def test_section_excludes_threads_and_processes_without_a_flock(tmp_path, monkeypatch, kind):
    lock, _path = lock_case(tmp_path, kind)
    guard = LockGuard(monkeypatch)
    probed, entered = Event(), Event()
    acquire = state_claims.acquire

    def observed(path):
        token = acquire(path)
        if token is None:
            probed.set()
        return token

    monkeypatch.setattr(state_claims, "acquire", observed)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "2")

    def contender():
        with lock():
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock():
            assert not guard.holding()
            waiting = pool.submit(contender)
            assert probed.wait(2), "the other thread did not encounter our token"
            assert not entered.is_set()
            assert child(tmp_path, kind, "try").stdout.strip() == "excluded"
        waiting.result(timeout=3)
    assert entered.is_set()
    assert child(tmp_path, kind, "try").stdout.strip() == "entered"
    assert not guard.violations, guard.report()


@pytest.mark.parametrize("kind", KINDS)
def test_section_reclaims_a_dead_process_and_a_reused_pid(tmp_path, kind):
    lock, path = lock_case(tmp_path, kind)
    assert child(tmp_path, kind, "die").returncode == 0
    intent = path.with_suffix(".intent")
    dead = json.loads(intent.read_text())
    assert dead["pid"] != os.getpid()
    with lock():
        assert json.loads(intent.read_text())["token"] != dead["token"]
    intent.write_text(json.dumps({"pid": os.getpid(), "start_time": 0, "token": "reused"}))
    with lock():
        assert json.loads(intent.read_text())["token"] != "reused"
    assert not intent.exists()


@pytest.mark.parametrize("kind", KINDS)
def test_section_timeout_reports_the_token_holder_without_flock_or_waiting_again(tmp_path, monkeypatch, kind):
    lock, path = lock_case(tmp_path, kind)
    intent = path.with_suffix(".intent")
    token = state_claims.acquire(intent)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0.02")
    guard = LockGuard(monkeypatch)
    try:
        with file_locks.reporting(tmp_path), pytest.raises(file_locks.LockTimeout, match="0.02s"):
            with lock():
                pytest.fail("entered a busy section")
    finally:
        state_claims.release(intent, token)
    [event] = orch_events.events(StateStore(tmp_path), "lock-timeout")
    assert event["lock"] == str(path) and event["wait_seconds"] == 0.02
    assert event["holders"][0]["token"] == token
    assert event["holders"][0]["pid"] == os.getpid()
    assert not guard.violations, guard.report()


@pytest.mark.parametrize("kind", ("merge", "publish"))
def test_section_reenters_on_its_thread_and_releases_on_exception(tmp_path, monkeypatch, kind):
    lock, path = lock_case(tmp_path, kind)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0.05")
    with pytest.raises(ValueError, match="unwind"):
        with lock():
            token = json.loads(path.with_suffix(".intent").read_text())["token"]
            with lock():
                assert json.loads(path.with_suffix(".intent").read_text())["token"] == token
            assert child(tmp_path, kind, "try").stdout.strip() == "excluded"
            raise ValueError("unwind")
    assert child(tmp_path, kind, "try").stdout.strip() == "entered"


def test_tick_and_worktree_probes_keep_their_existing_reentry_semantics(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    guard = LockGuard(monkeypatch)
    with store.tick_lock() as acquired:
        assert acquired and not guard.holding()
        with store.tick_lock() as nested:
            assert nested is False
        with store.hold_tick_lock() as nested:
            assert nested is False
    with store.hold_tick_lock() as acquired:
        assert acquired
        with store.hold_tick_lock() as nested, store.tick_lock() as tick:
            assert nested and tick
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other_tick():
                with store.tick_lock() as owned:
                    return owned
            assert pool.submit(other_tick).result(timeout=2) is False
    with store.worktree_lock(1):
        with store.try_worktree_lock(1) as owned:
            assert owned is False
        with pytest.raises(file_locks.LockTimeout):
            with store.worktree_lock(1, timeout=0):
                pytest.fail("busy worktree")
    with store.try_worktree_lock(1) as owned:
        assert owned and not guard.holding()
    assert not orch_events.events(store, "lock-timeout")


def test_token_sections_enforce_route_merge_publish_order(tmp_path):
    merge, _ = lock_case(tmp_path, "merge")
    publish, _ = lock_case(tmp_path, "publish")
    route = lambda: file_locks.file_lock(tmp_path / "route.lock")
    with route(), merge(), publish():
        pass
    for outer, inner in ((merge, route), (publish, merge), (publish, route)):
        with outer(), pytest.raises(RuntimeError, match="Lock order inversion"):
            with inner():
                pytest.fail("inversion entered")
    with StateStore(tmp_path)._state_lock(), pytest.raises(RuntimeError, match="Lock order inversion"):
        with merge():
            pytest.fail("state to merge inversion")
    assert file_locks._HELD.get() == ()


@pytest.mark.parametrize("module", (merge_owner, order_publications_process))
def test_scheduler_style_start_spawns_under_tick_without_flock(tmp_path, monkeypatch, module):
    store = StateStore(tmp_path / "store")
    ctx = SimpleNamespace(config=Config(merge_owner=True), store=store, repo="o/r", repo_root=tmp_path,
                          now=lambda: datetime.now(UTC))
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {"queued": True})
    from foragent import order_publications
    monkeypatch.setattr(order_publications, "pending", lambda store: ["pending"])
    popen, children = subprocess.Popen, []

    def launch(*args, **kwargs):
        process = popen(["true"])
        children.append(process)
        return process

    guard = LockGuard(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", launch)
    report = TickReport()
    with store.tick_lock() as acquired:
        assert acquired
        module.start(ctx, report)
        for process in children:
            process.wait()
    assert len(children) == 1 and any("started" in action for action in report.actions)
    assert guard.starts and not guard.violations, guard.report()


def fork_result(work):
    """A real fork with pipes, bounded lock waits and no child pytest teardown."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        try:
            try:
                result = work()
            except BaseException as exc:
                result = type(exc).__name__
            os.write(writer, result.encode())
        finally:
            os.close(writer)
            os._exit(0)
    os.close(writer)
    try:
        result = os.read(reader, 4096).decode()
    finally:
        os.close(reader)
        assert os.waitpid(pid, 0)[1] == 0
    return result


@pytest.mark.parametrize("kind", ("merge", "publish", "tick"))
def test_fork_cannot_reuse_parent_thread_ownership_or_ordering(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0.05")
    lock, path = lock_case(tmp_path, kind)
    guard = LockGuard(monkeypatch)

    def attempt():
        try:
            with lock() as acquired:
                return "excluded" if acquired is False else "entered"
        except file_locks.LockTimeout:
            return "excluded"

    with lock():
        token = json.loads(path.with_suffix(".intent").read_text())["token"]
        assert fork_result(attempt) == "excluded"
        assert json.loads(path.with_suffix(".intent").read_text())["token"] == token
    assert not guard.violations, guard.report()


@pytest.mark.parametrize("kind", (*KINDS, "tick"))
def test_fork_unwinding_an_inherited_section_cannot_release_the_parent(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0.05")
    lock, path = lock_case(tmp_path, kind)
    guard = LockGuard(monkeypatch)
    # Unwinding an inherited inner section must not restore the parent's outer
    # ordering context either: this child must wait for merge, not reenter it.
    with StateStore(tmp_path).merge_lock() if kind == "publish" else nullcontext():
        ownership = lock()
        ownership.__enter__()
        try:
            token = json.loads(path.with_suffix(".intent").read_text())["token"]

            def unwind():
                ownership.__exit__(None, None, None)
                if kind == "publish":
                    try:
                        with StateStore(tmp_path).merge_lock():
                            return "entered"
                    except file_locks.LockTimeout:
                        pass
                return "closed"

            assert fork_result(unwind) == "closed"
            assert json.loads(path.with_suffix(".intent").read_text())["token"] == token
        finally:
            ownership.__exit__(None, None, None)
    assert not path.with_suffix(".intent").exists()
    assert not guard.violations, guard.report()

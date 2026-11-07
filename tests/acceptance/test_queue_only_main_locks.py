"""M2 for queue-only-main (PM q62 B): no subprocess, sleep or process wait under a held lock.

Statement: while any foragent file lock is held, no subprocess is started, no sleep happens and
no wait for another process or the network begins; a test that does any of these under a held
lock fails.

The guard below watches the calling thread only. A Foragent lock is a ``fcntl.flock`` taken by
code in a ``foragent`` module (``file_locks.acquire`` and the modules that lock directly); it is
held until that thread unlocks it, confirmed by a nonblocking probe from a separate descriptor.
One kind is not a lock in this sense: a machine test slot (``capacity.hold_test_slot``, the files
``slot-N.lock`` in the machine's test-slot folder) is a capacity token that exists to be held for
the whole test command it admits. The slots' guard lock and every other lock are counted.
While the thread holds one:

- a subprocess start (``subprocess.Popen._execute_child``: every ``subprocess.run``, ``git`` call
  and ``gh`` call goes through it) is recorded;
- ``time.sleep`` and a blocking ``os.waitpid`` (process waits, ``Popen.wait``, multiprocessing
  joins) are recorded. A sleep under a lock is cut to 0.05 s so no wall time is spent; nothing is
  interrupted, so ``subprocess`` never has to kill a child (no signal is sent).

Network calls under a lock are covered by test_queue_only_main.py (statement 6); this file pins
the rest of M2. The scenarios are the same public entry points as there: `fa order publish`,
`fa order publish --defer` + `fa order note --defer` + `fa order flush`, a red check that returns
an order, and `fa order prepare`'s claim, each followed by gate passes (red checker, then the
merge owner). Git is a real local origin; GitHub is the fake tracker; no signal is sent.
"""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from foragent import file_locks, order_prepare_owner
from queue_only_main_world import world  # noqa: F401  (fixture)
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401
from test_queue_only_main import lane_ready, scenario_flush, scenario_publish, scenario_returned

HERE = {__name__, "queue_only_main_world"}
# The Popen class itself (autouse fixtures later replace the module attribute with functions).
POPEN = subprocess.Popen
assert isinstance(POPEN, type)


def _fd_path(fd):
    try:
        if sys.platform == "darwin":
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0"))
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _is_test_slot(path):
    from foragent import capacity

    folder = Path(capacity.machine_root()).resolve() / capacity.TEST_SLOTS_DIR
    return Path(path).resolve().parent == folder and Path(path).name.startswith("slot-")


def _caller_module():
    frame = sys._getframe(2)
    while frame is not None and frame.f_globals.get("__name__", "") in HERE:
        frame = frame.f_back
    return frame.f_globals.get("__name__", "") if frame is not None else ""


class LockGuard:
    """Records subprocess starts, sleeps and process waits made while this thread holds a lock."""

    def __init__(self, monkeypatch):
        self.real_flock = fcntl.flock
        self.real_sleep = time.sleep
        self.real_waitpid = os.waitpid
        self.real_execute = POPEN._execute_child
        self.held = {}          # thread id -> {fd: path}
        self.violations = []
        self.starts = []        # every subprocess start, held or not (proves the scenario ran some)
        self.mutex = threading.RLock()
        self.active = True
        guard = self

        def flock(fd, operation):
            guard.real_flock(fd, operation)
            number = fd if isinstance(fd, int) else fd.fileno()
            module = _caller_module()
            with guard.mutex:
                mine = guard.held.setdefault(threading.get_ident(), {})
                if operation & fcntl.LOCK_UN:
                    mine.pop(number, None)
                elif module == "foragent" or module.startswith("foragent."):
                    path = _fd_path(number)
                    if path is not None and not _is_test_slot(path):
                        mine[number] = path

        def sleep(seconds):
            if guard.check(f"sleep({seconds})"):
                seconds = min(seconds, 0.05)
            return guard.real_sleep(seconds)

        def waitpid(pid, options):
            if not options & os.WNOHANG:
                guard.check(f"waitpid({pid})")
            return guard.real_waitpid(pid, options)

        def execute(popen, args, *rest, **kwargs):
            shown = " ".join(str(part) for part in args[:3]) if isinstance(args, (list, tuple)) else str(args)
            guard.starts.append(shown)
            guard.check(f"subprocess {shown}")
            return guard.real_execute(popen, args, *rest, **kwargs)

        monkeypatch.setattr(fcntl, "flock", flock)
        monkeypatch.setattr(time, "sleep", sleep)
        monkeypatch.setattr(os, "waitpid", waitpid)
        monkeypatch.setattr(POPEN, "_execute_child", execute)

    def holding(self):
        """Locks this thread still holds, each proven by a nonblocking probe."""
        held = []
        with self.mutex:
            mine = self.held.get(threading.get_ident(), {})
            for number, path in list(mine.items()):
                try:
                    probe = os.open(path, os.O_RDONLY)
                except OSError:
                    mine.pop(number, None)
                    continue
                try:
                    self.real_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(Path(path).name)
                else:
                    self.real_flock(probe, fcntl.LOCK_UN)
                    mine.pop(number, None)
                finally:
                    os.close(probe)
        return sorted(held)

    def check(self, what):
        if not self.active:
            return False
        held = self.holding()
        if held:
            self.violations.append(f"{what} while holding {', '.join(held)}")
        return bool(held)

    def report(self):
        return "\n".join(sorted(set(self.violations)))


def run_guarded(world, monkeypatch, scenario):
    guard = LockGuard(monkeypatch)
    failure = None
    try:
        result = scenario(world)
    except BaseException as exc:  # noqa: BLE001 - the guard's verdict comes first
        failure, result = exc, None
    finally:
        guard.active = False
    assert guard.violations == [], (
        "subprocess starts, sleeps or process waits while a Foragent file lock was held:\n" + guard.report())
    if failure is not None:
        raise failure
    return guard, result


# The guard itself: it fails a test that does these things under a lock, and only then. ---------

def test_the_guard_fails_a_subprocess_a_sleep_and_a_process_wait_under_a_held_lock(tmp_path, monkeypatch):
    guard = LockGuard(monkeypatch)
    lock = tmp_path / "state" / "state.lock"
    subprocess.run(["true"], check=True)
    time.sleep(0)
    assert guard.violations == [], "nothing is recorded while no lock is held"
    process = subprocess.Popen(["true"])
    with file_locks.file_lock(lock):
        started = time.monotonic()
        time.sleep(30)
        slept = time.monotonic() - started
        os.waitpid(process.pid, 0)
    assert slept < 5, "a sleep under a lock is recorded, not spent"
    assert [entry.split(" while ")[0] for entry in guard.violations] == [
        "sleep(30)", f"waitpid({process.pid})"], guard.violations
    with file_locks.file_lock(lock):
        subprocess.run(["true"], check=True)
    assert len(guard.violations) == 4, guard.violations   # The start and its wait, once each.
    assert guard.violations[2].startswith("subprocess true") and "state.lock" in guard.violations[2]
    assert guard.violations[3].startswith("waitpid") and "state.lock" in guard.violations[3]
    # After the lock is released, the same calls are not recorded again.
    subprocess.run(["true"], check=True)
    time.sleep(0)
    assert len(guard.violations) == 4
    guard.active = False


def test_the_guard_ignores_a_lock_another_thread_holds(tmp_path, monkeypatch):
    guard = LockGuard(monkeypatch)
    lock = tmp_path / "state" / "state.lock"
    taken, release = threading.Event(), threading.Event()

    def hold():
        with file_locks.file_lock(lock):
            taken.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(10)
    try:
        subprocess.run(["true"], check=True)
        time.sleep(0)
    finally:
        release.set()
        holder.join(10)
    assert guard.violations == [], guard.violations
    guard.active = False


# The M2 statement over the queue-only-main entry points. ----------------------------------------

@pytest.mark.parametrize("scenario", [scenario_publish, scenario_flush, scenario_returned])
def test_no_subprocess_sleep_or_process_wait_while_a_foragent_lock_is_held(world, monkeypatch, scenario):
    guard, published = run_guarded(world, monkeypatch, scenario)
    assert any(start.startswith("git") for start in guard.starts), "the scenario must run git (the guard saw none)"
    for plan in published:
        lane_ready(world, plan)
    assert world.lock_timeouts() == []


def test_a_preparation_claim_starts_no_subprocess_and_does_not_sleep_under_its_lock(world, monkeypatch):
    folder = world.store.root / "prepare"

    def claim(box):
        first = order_prepare_owner.claim(folder, {"plan": "claimed-order", "issues": {}}, ["issue"], lambda: None)
        again = order_prepare_owner.claim(folder, {"plan": "claimed-order", "issues": {}}, ["issue"], lambda: None)
        return [first, again]

    _guard, results = run_guarded(world, monkeypatch, claim)
    assert results[0] is True, "the preparation claim is taken"

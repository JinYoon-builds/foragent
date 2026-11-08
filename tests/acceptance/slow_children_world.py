"""A deterministic slow runner: every child process takes ``delay`` seconds on fake clocks.

Nothing here waits for real and nothing here sends a real signal. The world models a loaded CI
runner where each child process (git, a fake executor) needs ``delay`` seconds to finish:

- ``time.monotonic`` and ``time.time`` read real time plus the seconds that slow children have
  consumed so far, so deadlines and budgets measured on those clocks see the slowness.
- A wait on a child (``Popen.wait`` / ``Popen.communicate``, and so ``subprocess.run``) with a
  timeout below the child's remaining time raises ``subprocess.TimeoutExpired`` after charging
  that timeout, exactly as a real slow child would; the child stays alive (``poll`` returns None)
  until its remaining time is charged.
- ``threading.Timer`` runs on the same fake clock: a timer whose interval passes while a slow
  child is being waited for fires then, synchronously, and its errors are recorded.
- ``os.kill`` / ``os.killpg`` with a non-zero signal are never delivered. Each attempt is
  recorded in ``signals`` and the targeted child counts as killed (exit -9), as the real child
  would be. With ``to_suite_guard=True`` the attempt then also fails the way the suite's own guard
  (tests/conftest.py ``no_real_signals``) fails it on CI; no signal API is called.
  Signal 0 probes always go to the suite's own guard unchanged.

``delay`` can be changed during a test; ``delay = 0`` is the ordinary fast world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import subprocess
import threading
import time
from typing import Any, Callable

import pytest


def _real_popen_class() -> type:
    """The real Popen class beneath the suite's function wrappers around ``subprocess.Popen``."""
    seen = set()

    def find(item: Any) -> type | None:
        if isinstance(item, type) and issubclass(item, object) and hasattr(item, "communicate"):
            return item
        if id(item) in seen or not callable(item):
            return None
        seen.add(id(item))
        for cell in getattr(item, "__closure__", None) or ():
            try:
                found = find(cell.cell_contents)
            except ValueError:
                continue
            if found is not None:
                return found
        return None

    found = find(subprocess.Popen)
    assert found is not None, "Could not find the real subprocess.Popen class"
    return found


@dataclass
class SlowChildren:
    delay: float
    offset: float = 0.0
    signals: list[tuple[str, int, int]] = field(default_factory=list)
    timeouts: list[tuple[list[str], float]] = field(default_factory=list)
    timer_errors: list[BaseException] = field(default_factory=list)
    children: list[list[str]] = field(default_factory=list)
    _timers: list["FakeTimer"] = field(default_factory=list)
    _procs: dict[int, Any] = field(default_factory=dict)

    # Fake clocks ---------------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        """Move the fake clocks on by ``seconds``, firing timers that fall due on the way."""
        end = self.offset + max(0.0, seconds)
        while True:
            due = sorted((timer for timer in self._timers if timer.due <= end), key=lambda t: t.due)
            if not due:
                break
            timer = due[0]
            self._timers.remove(timer)
            self.offset = max(self.offset, timer.due)
            try:
                timer.function(*timer.args, **timer.kwargs)
            except BaseException as exc:  # noqa: BLE001 - a timer runs in its own thread for real
                self.timer_errors.append(exc)
        self.offset = end

    # Children ------------------------------------------------------------------------------
    def _remaining(self, process) -> float:
        return getattr(process, "_slow_remaining", 0.0)

    def charge(self, process, timeout: float | None) -> None:
        """Spend the child's remaining time, or raise TimeoutExpired once ``timeout`` runs out."""
        remaining = self._remaining(process)
        if remaining <= 0:
            return
        if timeout is not None and timeout < remaining:
            process._slow_remaining = remaining - max(0.0, timeout)
            self.timeouts.append((list(process.args) if not isinstance(process.args, str) else [process.args],
                                  timeout))
            self.advance(max(0.0, timeout))
            raise subprocess.TimeoutExpired(process.args, timeout)
        self.advance(remaining)
        process._slow_remaining = 0.0

    def kill_attempt(self, name: str, target: int, sig: int) -> None:
        self.signals.append((name, target, sig))
        for process in list(self._procs.values()):
            if process.pid == target:  # A child started in its own group has pgid == pid.
                process._slow_killed = True
                process._slow_remaining = 0.0


class FakeTimer:
    """``threading.Timer`` on the slow world's fake clock; never starts a thread."""

    world: SlowChildren

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval, self.function = float(interval), function
        self.args, self.kwargs = list(args or ()), dict(kwargs or {})
        self.due = float("inf")
        self.daemon = True
        self.name = "fake-timer"

    def start(self):
        self.due = self.world.offset + self.interval
        self.world._timers.append(self)

    def cancel(self):
        if self in self.world._timers:
            self.world._timers.remove(self)

    def is_alive(self):
        return self in self.world._timers

    def join(self, timeout=None):
        return None


def install(monkeypatch, delay: float, *, to_suite_guard: bool = False) -> SlowChildren:
    """Install the slow world for the rest of the test and return it."""
    world = SlowChildren(delay=delay)
    popen = _real_popen_class()
    real_init, real_wait = popen.__init__, popen.wait
    real_communicate, real_poll = popen.communicate, popen.poll

    def init(self, args, *positional, **kwargs):
        real_init(self, args, *positional, **kwargs)
        self._slow_remaining = float(world.delay)
        self._slow_killed = False
        world._procs[id(self)] = self
        world.children.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])

    def killed(self) -> bool:
        return getattr(self, "_slow_killed", False)

    def finish(self) -> None:
        world._procs.pop(id(self), None)
        if killed(self):
            self.returncode = -9

    def wait(self, timeout=None):
        world.charge(self, timeout)
        # The child's time is spent on the fake clock; the real child is only reaped.
        real_wait(self)
        finish(self)
        return self.returncode

    def communicate(self, input=None, timeout=None):
        world.charge(self, timeout)
        output, errors = real_communicate(self, input, None)
        finish(self)
        if killed(self):
            output = output[:0] if output is not None else None
            errors = errors[:0] if errors is not None else None
        return output, errors

    def poll(self):
        if getattr(self, "_slow_remaining", 0.0) > 0 and not killed(self):
            return None
        result = real_poll(self)
        if result is not None:
            finish(self)
        return self.returncode

    monkeypatch.setattr(popen, "__init__", init)
    monkeypatch.setattr(popen, "wait", wait)
    monkeypatch.setattr(popen, "communicate", communicate)
    monkeypatch.setattr(popen, "poll", poll)

    monotonic, wall = time.monotonic, time.time
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + world.offset)
    monkeypatch.setattr(time, "time", lambda: wall() + world.offset)

    FakeTimer.world = world
    monkeypatch.setattr(threading, "Timer", FakeTimer)

    def guarded(name: str, previous: Callable[..., Any]):
        def send(target, sig, *args, **kwargs):
            if sig == 0:
                return previous(target, sig, *args, **kwargs)
            world.kill_attempt(name, target, int(sig))
            if to_suite_guard:
                # What the suite guard does on CI, without ever calling a signal API here.
                pytest.fail(f"Real delivering signals are forbidden: {(target, sig)!r}")
            return None
        return send

    # Installed above the suite's own guard: probes pass through, deliveries never do.
    monkeypatch.setattr(os, "kill", guarded("kill", os.kill))
    monkeypatch.setattr(os, "killpg", guarded("killpg", os.killpg))
    return world


@pytest.fixture
def slow_children(no_real_signals, monkeypatch):
    """Factory fixture: ``slow_children(delay)`` installs the slow world for this test."""
    return lambda delay, **options: install(monkeypatch, delay, **options)

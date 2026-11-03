"""The owner-side gate of a project stop (#1491): after a stop, a run takes no external step.

A run owner (the issue runner, a review rerun, the dream worker) enters `owner()` with a probe
that reads the stop in force. While it is inside, every external step of that process asks
`check()` at the one place that step passes: each GitHub call through the run's tracker
(`Guarded`), each remote Git command (`GitRepo._call`), each check command (`run_checks._check`,
after its test-slot wait and again after the command), and each wait's sleep. A stop raises
ExecutorStopped("stopped") there, so the owner leaves through its own save-and-finish path.
The save itself runs inside `saving()`, which lets the WIP commit and push through.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

_probes: list[Callable[[], str | None]] = []
_saving = [0]


@contextmanager
def owner(probe: Callable[[], str | None]) -> Iterator[None]:
    """This process owns a run until the block ends; `probe` returns the why of a stop in force."""
    _probes.append(probe)
    try:
        yield
    finally:
        _probes.remove(probe)


@contextmanager
def saving() -> Iterator[None]:
    """The owner's save of unfinished work: its reads, commit and push pass the gate."""
    _saving[0] += 1
    try:
        yield
    finally:
        _saving[0] -= 1


def check() -> None:
    """Raise ExecutorStopped("stopped") when a run owner of this process is under a stop."""
    if not _probes or _saving[0]:
        return
    if (why := _probes[-1]()) is not None:
        from foragent.run_attempt import ExecutorStopped

        raise ExecutorStopped("stopped", why)


class Guarded:
    """A tracker whose every call, read or write, asks the gate first."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        value = getattr(self._inner, name)
        if not callable(value):
            return value

        def gated(*args, **kwargs):
            check()
            return value(*args, **kwargs)

        return gated

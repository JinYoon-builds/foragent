"""The shared boundary for gh subprocess calls and their local per-hour count.

A tick or run context installs a recorder (its state store and clock) when it
wraps its tracker; every gh attempt made afterwards in this process adds one to
the store's counter for the current UTC hour. Without a recorder nothing counts.
"""

from collections.abc import Callable
from datetime import datetime
import subprocess
from typing import Any

_recorder: tuple[Any, Callable[[], datetime]] | None = None


def set_recorder(store: Any, now: Callable[[], datetime]) -> None:
    global _recorder
    _recorder = (store, now)


def clear_recorder() -> None:
    global _recorder
    _recorder = None


def count_call() -> None:
    """Count one gh attempt, whether it later succeeds, fails or times out."""
    recorder = _recorder
    if recorder is not None:
        store, now = recorder
        store.count_github_call(now())


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run ``gh <args>`` with subprocess.run keyword arguments, counting the attempt."""
    count_call()
    return subprocess.run(["gh", *args], **kwargs)

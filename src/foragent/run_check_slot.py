"""The Stop hook's Done when commands wait for the shared machine test slot (#993).

The hook runs inside a worker that the gate already counts, so it takes no
machine workload of its own: only the test slot every other test command uses.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import os
from pathlib import Path
import time

from foragent import capacity
from foragent.config import Config, ConfigError, load_config

# Execution time each Done when command may use inside the hook (run_checks.check_done).
COMMAND_SECONDS = 900


def stop_hook_slot(worktree: Path, commands: int) -> Callable[[], AbstractContextManager[None]]:
    """One test-slot hold per command, all bounded by one monotonic deadline.

    The deadline leaves each command its execution time inside the hook timeout
    that run_execution sets, so a slot wait cannot make the hook itself time out
    (which would let the stop through). Past it, TimeoutError (an OSError) fails
    the check and the hook blocks the stop.
    """
    try:
        config = load_config(worktree)
    except (OSError, ConfigError):
        config = Config()
    hook_seconds = max(COMMAND_SECONDS, config.max_runtime_minutes * 60)
    deadline = time.monotonic() + max(0, hook_seconds - COMMAND_SECONDS * commands)

    @contextmanager
    def slot() -> Iterator[None]:
        with capacity.hold_test_slot(
            config.test_slots, deadline=deadline,
            timed_out=lambda: "No machine test slot became free before the Stop hook's deadline.",
        ):
            # The command (and a nested `fa test` in it) shares this slot instead of waiting for another.
            previous = os.environ.get(capacity.TEST_SLOT_ENV)
            os.environ.update(capacity.HELD_ENV)
            try:
                yield
            finally:
                if previous is None:
                    os.environ.pop(capacity.TEST_SLOT_ENV, None)
                else:
                    os.environ[capacity.TEST_SLOT_ENV] = previous

    return slot

"""The Stop hook's Done when commands wait for the shared machine test slot (#993).

The hook runs inside a worker that the gate already counts, so it takes no
machine workload of its own: only the test slot every other test command uses.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from typing import Any

from foragent import capacity
from foragent.config import Config, ConfigError, load_config

# The most execution time one Done when command gets inside the hook.
COMMAND_SECONDS = 900
# Kept free inside the hook timeout for process start, reporting and exit.
MARGIN_SECONDS = 30
SETTINGS_FILE = "claude-settings.json"


def hook_seconds(config: Any) -> int:
    """The Stop hook timeout run_execution writes into the Claude settings."""
    return max(COMMAND_SECONDS, int(config.max_runtime_minutes) * 60)


def _settings_timeout(directory: Path) -> int | None:
    """The timeout Claude enforces on this hook, as written for this run."""
    try:
        settings = json.loads((directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        return int(settings["hooks"]["Stop"][0]["hooks"][0]["timeout"])
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return None


class StopHookBudget:
    """One monotonic deadline for the whole hook: every slot wait and every command fits inside it.

    Claude kills a hook that outlives its timeout and then lets the stop through, so the
    hook must finish first: each command gets what is left after the slot wait (at most
    COMMAND_SECONDS). Past the deadline, TimeoutError (an OSError) fails the check and the
    hook blocks the stop.
    """

    def __init__(self, directory: Path, worktree: Path) -> None:
        try:
            config = load_config(worktree)
        except (OSError, ConfigError):
            config = Config()
        self.test_slots = config.test_slots
        total = _settings_timeout(directory)
        total = hook_seconds(config) if total is None else total
        self.deadline = time.monotonic() + max(0, total - MARGIN_SECONDS)

    def timeout(self) -> int:
        """Seconds the next command may run; none left means the check failed."""
        left = int(self.deadline - time.monotonic())
        if left <= 0:
            raise TimeoutError("The Stop hook has no time left for this Done when command.")
        return min(COMMAND_SECONDS, left)

    @contextmanager
    def slot(self) -> Iterator[None]:
        with capacity.hold_test_slot(
            self.test_slots, deadline=self.deadline,
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

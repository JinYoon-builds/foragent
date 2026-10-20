"""Launch an independent runner with a per-issue append-only log."""

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys

from foragent.state import StateStore


def spawn_runner(repo_root: Path, store: StateStore) -> Callable[[int], int]:
    def launch(number: int) -> int:
        log = store.log_path(number)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "foragent.cli", "gate", "run-issue", str(number),
                 "--repo-root", str(repo_root)],
                start_new_session=True, stdout=stream, stderr=stream,
            )
        return process.pid

    return launch

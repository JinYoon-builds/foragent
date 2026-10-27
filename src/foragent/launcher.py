"""Launch independent runners and notifications with append-only logs."""

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import sys

from foragent.state import StateStore
from foragent.processes import observe_tree


def gate_environment() -> dict[str, str]:
    """Gate children write no bytecode, so a tool reinstall can always empty its directory."""
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def spawn_notify(repo_root: Path, store: StateStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / "notify.log").open("ab") as stream:
        subprocess.Popen(
            [sys.executable, "-m", "foragent.cli", "inspect", "notify",
             "--repo-root", str(repo_root)],
            stdin=subprocess.DEVNULL, start_new_session=True, stdout=stream, stderr=stream,
            env=gate_environment(),
        )


def spawn_runner(repo_root: Path, store: StateStore) -> Callable[..., int]:
    def launch(number: int | None = None, *, kind: str = "issue", lane: str | None = None) -> int:
        dream = kind == "dream"
        log = store.root / "logs" / f"dream-{lane}.log" if dream else store.log_path(number)
        log.parent.mkdir(parents=True, exist_ok=True)
        action = ["run-dream", str(lane)] if dream else ["run-issue", str(number)]
        with log.open("ab") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "foragent.cli", "gate", *action, "--repo-root", str(repo_root)],
                start_new_session=True, stdout=stream, stderr=stream, env=gate_environment(),
            )
            # Capture while Popen still holds the unreaped child. Returning just
            # its PID would let a later state update adopt a reused number.
            try:
                identities = observe_tree(roots=(process.pid,))
            except PermissionError:
                identities = []  # A stop must refuse without birth evidence.
            store.update_run(f"dream-{lane}" if dream else number, pid=process.pid, process_identities=identities)
        return process.pid

    return launch

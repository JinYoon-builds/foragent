"""Talk terminal records, probes and endings for `fa lanes talk` and the gate tick (D55 addendum).

A running issue's talk terminal is recorded in ``<state>/talk/<n>.json``. While
it stays open the run is ``talking``: the tick pauses its stall and runtime
checks, and the first tick that confirms the terminal closed ends the talk.
Opening terminals and sending messages live in ``lane_talk_open``.
"""

from __future__ import annotations

import json
import socket as pysocket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from foragent.state import StateStore
from foragent.state_records import Run

OPEN_TIMEOUT = 10


def talk_path(store: StateStore, number: int) -> Path:
    return store.root / "talk" / f"{number}.json"


def _call(argv: list[str], cwd: Path, timeout: int = OPEN_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=timeout)


# Orca's error codes that say the handle no longer names a terminal (``terminal show``
# of a closed terminal answers ok:false with this code).
ORCA_ABSENT = frozenset({"terminal_handle_stale", "terminal_not_found"})


def _orca_envelope(text: str | None) -> dict | None:
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _orca_handle(stdout: str) -> str | None:
    """The new terminal's handle: ``result.terminal.handle`` in Orca's envelope, never its request ``id``.

    A bare object with a top-level ``handle`` (an older or fake Orca) is read too.
    """
    data = _orca_envelope(stdout)
    if data is None or data.get("ok") is False:
        return None
    if "result" in data:
        result = data["result"]
        terminal = result.get("terminal") if isinstance(result, dict) else None
        handle = terminal.get("handle") if isinstance(terminal, dict) else None
    else:
        handle = data.get("handle")
    return handle if isinstance(handle, str) and handle else None


def _tmux_server_up(socket_path: str) -> bool | None:
    """Whether a tmux server listens on ``socket_path``: False when nothing can (gone or refused)."""
    probe = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(socket_path)
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return None
    finally:
        probe.close()


def _orca_listed(executable: str, handle: str, cwd: Path) -> bool | None:
    """After a ``terminal show`` failure without an envelope: is the handle among Orca's terminals?

    None when Orca cannot list either (it is down or crashing), so nothing is concluded.
    When the list answers in Orca's envelope, membership decides. A successful list in
    another shape shows Orca is working, so the failed show is its answer for this handle.
    """
    try:
        data = _orca_envelope(_call([executable, "terminal", "list", "--json"], cwd).stdout)
    except (OSError, subprocess.SubprocessError):
        return None
    if data is None or data.get("ok") is False:
        return None
    result = data.get("result")
    terminals = result.get("terminals") if isinstance(result, dict) else None
    if isinstance(terminals, list):
        return any(isinstance(item, dict) and item.get("handle") == handle for item in terminals)
    return False


def _alive(record: dict, cwd: Path, *, show: bool) -> bool | None:
    """Whether a recorded terminal is open: True, False (confirmed closed) or None (could not tell).

    A probe that times out, cannot start, or fails for another reason is not
    evidence that the terminal closed. ``show`` also brings an open terminal forward.
    """
    executable, handle = record.get("executable"), record.get("handle")
    if not isinstance(executable, str) or not isinstance(handle, str):
        return False
    if record.get("kind") == "orca":
        try:
            answer = _call([executable, "terminal", "show", "--terminal", handle, "--json"], cwd).stdout
        except subprocess.CalledProcessError as exc:
            envelope = _orca_envelope(exc.stdout)
            if envelope is not None:  # Orca answered: its own error code decides.
                error = envelope.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                return False if code in ORCA_ABSENT else None
            if exc.returncode < 0:
                return None  # The CLI was killed by a signal: no answer at all.
            # No envelope (a crash, or a stderr-only reply): ask Orca a second, independent question.
            return _orca_listed(executable, handle, cwd)
        except (OSError, subprocess.SubprocessError):
            return None
        if (_orca_envelope(answer) or {}).get("ok") is False:
            return None
        if show:
            try:
                _call([executable, "terminal", "switch", "--terminal", handle], cwd)
            except (OSError, subprocess.SubprocessError):
                pass  # Still open; it just did not come forward.
        return True
    # The gate's tick has no TMUX of its own; the recorded server socket names the server.
    socket = record.get("socket") if isinstance(record.get("socket"), str) else None
    tmux = [executable, *(["-S", socket] if socket else [])]
    try:
        windows = _call([*tmux, "list-windows", "-a", "-F", "#{window_id}"], cwd).stdout.split()
    except subprocess.CalledProcessError:
        # tmux fails both when its server is gone and when it cannot ask; the socket tells which.
        return False if socket and _tmux_server_up(socket) is False else None
    except (OSError, subprocess.SubprocessError):
        return None
    if handle not in windows:
        return False
    if show:
        try:
            _call([*tmux, "select-window", "-t", handle], cwd)
        except (OSError, subprocess.SubprocessError):
            pass
    return True


class RecordUnreadable(Exception):
    """A talk record exists but cannot be read: its terminal may still be open."""


def _read_record(store: StateStore, number: int) -> dict | None:
    """The talk record; None only when there is none. An unreadable one raises ``RecordUnreadable``."""
    path = talk_path(store, number)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordUnreadable(str(path)) from exc
    if not (isinstance(record, dict) and isinstance(record.get("executable"), str)
            and isinstance(record.get("handle"), str)):
        raise RecordUnreadable(str(path))
    return record


@contextmanager
def unless_talking(store: StateStore, number: int) -> Iterator[bool]:
    """Hold the issue's talk lock while the tick stops a run for time; yields False when it must not.

    The tick decided from an earlier read. Under the lock a talk can neither open
    (talk marks the run talking under it) nor be half-opened, so the run is read
    again here: an open or opening talk pauses the stop.
    """
    with store.talk_lock(number, blocking=False) as held:
        run = store.get_run(number) if held else None
        yield run is not None and not run.talking


def check_talking(store: StateStore, run: Run, root: Path, now: datetime) -> tuple[Run, bool]:
    """Tick step for a talking run: ``(run, still_open)``; a closed terminal ends the talk.

    Ending adds the paused time to ``waited_seconds`` and restarts the stall
    clock (``last_progress_at``) at ``now``. A talk command in progress for the
    issue holds its lock; the tick then treats the talk as open and looks again
    next tick, so it never ends a talk that is being opened.
    """
    with store.talk_lock(run.number, blocking=False) as held:
        if not held:
            return run, True
        current = store.get_run(run.number)
        if current is None or current.status != "running" or not current.talking:
            return current or run, False
        try:
            record = _read_record(store, run.number)
        except RecordUnreadable:
            return current, True  # Not knowing the terminal is not knowing it closed.
        worktree = Path(current.worktree)
        if record is not None and _alive(record, worktree if worktree.is_dir() else root, show=False) is not False:
            return current, True  # Open, or the probe could not tell: only a confirmed closure ends a talk.
        return end_talk(store, current, now), False


def end_talk(store: StateStore, run: Run, now: datetime) -> Run:
    """Credit the paused time and drop the record in one state-lock step."""
    return store.end_talk(run.number, now) or run


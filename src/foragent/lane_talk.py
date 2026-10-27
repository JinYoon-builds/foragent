"""Talk terminal records, probes and endings for `fa lanes talk` and the gate tick (D55 addendum).

A running issue's talk terminal is recorded in ``<state>/talk/<n>.json``. While
it stays open the run is ``talking``: the tick pauses its stall and runtime
checks, and the first tick that confirms the terminal closed ends the talk.
Opening terminals and sending messages live in ``lane_talk_open``.
"""

from __future__ import annotations

import json
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


def _orca_closed(stdout: str | None) -> bool:
    """Orca's structured answer that the handle names no terminal: an ``ok:false`` stdout envelope
    whose error code says so, whatever the exit code."""
    data = _orca_envelope(stdout)
    error = data.get("error") if data is not None and data.get("ok") is False else None
    return isinstance(error, dict) and error.get("code") in ORCA_ABSENT


def find_pending(record: dict, cwd: Path) -> tuple[bool | None, str | None]:
    """A pending record (handle not saved yet): find its terminal by its unique title.

    Returns ``(True, handle)`` when a working listing (Orca's envelope with a terminal
    list, or tmux on the recorded socket) names the title, else ``(None, None)``. A listing
    without the title is not proof the terminal closed (it may be incomplete, or the title
    may have changed), and a pending record has no handle that Orca could call stale, so
    nothing here ever reports closed; such a talk ends with its run.
    """
    executable, title = record.get("executable"), record.get("title")
    if not isinstance(executable, str) or not isinstance(title, str):
        return None, None
    if record.get("kind") == "orca":
        try:
            data = _orca_envelope(_call([executable, "terminal", "list", "--json"], cwd).stdout)
        except (OSError, subprocess.SubprocessError):
            return None, None
        result = data.get("result") if data is not None and data.get("ok") is True else None
        terminals = result.get("terminals") if isinstance(result, dict) else None
        if not isinstance(terminals, list):
            return None, None
        for item in terminals:
            if isinstance(item, dict) and item.get("title") == title and isinstance(item.get("handle"), str):
                return True, item["handle"]
        return None, None
    socket = record.get("socket") if isinstance(record.get("socket"), str) else None
    if socket is None:
        return None, None
    try:
        lines = _call([executable, "-S", socket, "list-windows", "-a", "-F", "#{window_id}\t#{window_name}"],
                      cwd).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None, None
    for line in lines:
        window, _, name = line.partition("\t")
        if name == title and window:
            return True, window
    return None, None


def _alive(record: dict, cwd: Path, *, show: bool) -> bool | None:
    """Whether a recorded terminal is open: True, False (closed) or None (could not tell).

    Only structured evidence says closed: Orca's ``terminal_handle_stale`` /
    ``terminal_not_found`` envelope on stdout, or a working tmux ``list-windows``
    reply on the recorded server socket that leaves out the window. A timeout, a
    crash, a failing socket, any other error or an unrecognised answer tells nothing,
    and the talk stays open. ``show`` also brings an open terminal forward.
    """
    executable, handle = record.get("executable"), record.get("handle")
    if isinstance(executable, str) and not isinstance(handle, str) and isinstance(record.get("title"), str):
        return find_pending(record, cwd)[0]  # Pending: found by title, or nothing can be told.
    if not isinstance(executable, str) or not isinstance(handle, str):
        return None  # A record that names no terminal cannot be probed: no evidence either way.
    if record.get("kind") == "orca":
        try:
            answer = _call([executable, "terminal", "show", "--terminal", handle, "--json"], cwd).stdout
        except subprocess.CalledProcessError as exc:
            return False if _orca_closed(exc.stdout) else None
        except (OSError, subprocess.SubprocessError):
            return None
        if _orca_closed(answer):
            return False
        if (_orca_envelope(answer) or {}).get("ok") is False:
            return None
        if show:
            try:
                _call([executable, "terminal", "switch", "--terminal", handle], cwd)
            except (OSError, subprocess.SubprocessError):
                pass  # Still open; it just did not come forward.
        return True
    # The gate's tick has no TMUX of its own; only the recorded server socket names the server.
    socket = record.get("socket") if isinstance(record.get("socket"), str) else None
    tmux = [executable, *(["-S", socket] if socket else [])]
    try:
        windows = _call([*tmux, "list-windows", "-a", "-F", "#{window_id}"], cwd).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None  # A failing server or socket is not evidence the window is gone.
    if handle not in windows:
        # Without the recorded socket the reply may come from another server: not evidence.
        return False if socket else None
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
            and (isinstance(record.get("handle"), str) or isinstance(record.get("title"), str))):
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
        store.anchor_talk_sleep(run.number)  # Sleep credited from now on falls inside this talk.
        try:
            record = _read_record(store, run.number)
        except RecordUnreadable:
            return current, True  # Not knowing the terminal is not knowing it closed.
        # begin_talk writes the record with talking in one step, so a talking run without one
        # never got as far as opening a terminal; that absence is the talk command's own fact.
        if record is None:
            return end_talk(store, current, now), False
        cwd = Path(current.worktree) if Path(current.worktree).is_dir() else root
        if not isinstance(record.get("handle"), str):
            found, handle = find_pending(record, cwd)
            if found:  # The command died before saving the handle; the title found the terminal.
                store.publish_talk(run.number, current.started_at, "running", {**record, "handle": handle})
                return current, True
        else:
            found = _alive(record, cwd, show=False)
        if found is not False:
            return current, True  # Open, or the probe could not tell: only a confirmed closure ends a talk.
        return end_talk(store, current, now), False


def end_talk(store: StateStore, run: Run, now: datetime) -> Run:
    """Credit the paused time and drop the record in one state-lock step."""
    return store.end_talk(run.number, now) or run


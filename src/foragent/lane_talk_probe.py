"""The one place that judges a talk terminal from its opener's reply (D55 addendum, D95).

``liveness`` returns exactly one of open, closed or unknown, and returns closed only
for two exact shapes: Orca's ``terminal show`` stdout envelope whose error code is
``terminal_handle_stale`` or ``terminal_not_found`` (any exit code), or a tmux
``list-windows -a -F '#{window_id}'`` reply on the recorded socket that exits 0,
whose every non-empty line is a window id, and that leaves the recorded id out.
Everything else is unknown: unknown keeps the talk open and timeouts paused, and
never lets a second terminal open beside it. ``find_pending`` looks a terminal up
by its unique title (found or unknown, never closed). ``bring_forward`` is a
separate step callers run only after ``liveness`` said open.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"
OPEN_TIMEOUT = 10
ORCA_ABSENT = frozenset({"terminal_handle_stale", "terminal_not_found"})
WINDOW_ID = re.compile(r"^@[0-9]+$")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """The opener's reply: exit code and output, also when a wrapper raised it as a failure."""
    try:
        return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              check=False, timeout=OPEN_TIMEOUT)
    except subprocess.CalledProcessError as exc:
        return subprocess.CompletedProcess(argv, exc.returncode, exc.output, exc.stderr)


def _json(text: str | None):
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _names_handle(data, handle: str) -> bool:
    """An exit-0 Orca reply names ``handle``: inside an ``ok`` envelope, or as a flat object."""
    if not isinstance(data, dict):
        return False
    if "ok" in data or "result" in data:  # An envelope; a result key without ok is read as one too.
        if ("ok" in data and data["ok"] is not True) or not isinstance(data.get("result"), dict):
            return False
        result = data["result"]
        terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else {}
        candidates = (result.get("handle"), terminal.get("handle"), result.get("id"), terminal.get("id"))
    else:
        candidates = (data.get("handle"), data.get("id"), data.get("terminal"))
    return handle in {_text(value) for value in candidates}


def _orca_liveness(handle: str, cwd: Path) -> str:
    executable = shutil.which("orca")
    if not executable:
        return UNKNOWN
    try:
        reply = _run([executable, "terminal", "show", "--terminal", handle, "--json"], cwd)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    data = _json(reply.stdout)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("code") in ORCA_ABSENT:
        return CLOSED  # Whatever ok says and whatever the exit code.
    if reply.returncode != 0:
        return UNKNOWN
    return OPEN if _names_handle(data, handle) else UNKNOWN


def _tmux_liveness(socket: str, window: str, cwd: Path) -> str:
    executable = shutil.which("tmux")
    if not WINDOW_ID.match(window) or not executable or not Path(socket).exists():
        return UNKNOWN
    try:
        reply = _run([executable, "-S", socket, "list-windows", "-a", "-F", "#{window_id}"], cwd)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if reply.returncode != 0:
        return UNKNOWN
    lines = [line.rstrip() for line in reply.stdout.splitlines() if line.rstrip()]
    if not lines or not all(WINDOW_ID.match(line) for line in lines):
        return UNKNOWN  # Empty, or not wholly a window list: nothing can be told.
    return OPEN if window in lines else CLOSED


def liveness(event: dict, cwd: Path) -> str:
    """Open, closed or unknown for a talk-opened event's terminal (Orca handle or tmux window)."""
    try:
        if handle := _text(event.get("handle")):
            return _orca_liveness(handle, cwd)
        socket, window = _text(event.get("socket")), _text(event.get("window_id"))
        if socket and window:
            return _tmux_liveness(socket, window, cwd)
    except Exception:  # noqa: BLE001 - an unexpected failure tells nothing
        return UNKNOWN
    return UNKNOWN


def _orca_entries(data) -> list | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    if "ok" in data or "result" in data:
        result = data.get("result")
        if ("ok" in data and data["ok"] is not True) or not isinstance(result, dict):
            return None
        data = result
    terminals = data.get("terminals")
    return terminals if isinstance(terminals, list) else None


def _orca_find(title: str, cwd: Path) -> str | None:
    executable = shutil.which("orca")
    if not executable:
        return None
    reply = _run([executable, "terminal", "list", "--json"], cwd)
    entries = _orca_entries(_json(reply.stdout)) if reply.returncode == 0 else None
    if entries is None:
        return None
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        key = "title" if "title" in entry else "name" if "name" in entry else None
        if key is None:
            continue
        if not isinstance(entry[key], str):
            return None
        if entry[key] != title:
            continue
        terminal = entry.get("terminal") if isinstance(entry.get("terminal"), dict) else {}
        handle = next((value for value in (_text(entry.get("handle")), _text(entry.get("id")),
                                           _text(terminal.get("handle")), _text(terminal.get("id"))) if value),
                      None)
        if handle:  # A matching entry without a handle is skipped and does not count.
            found.append(handle)
    return found[0] if len(found) == 1 else None


def _tmux_find(socket: str, title: str, cwd: Path) -> str | None:
    executable = shutil.which("tmux")
    if not executable:
        return None
    reply = _run([executable, "-S", socket, "list-windows", "-a", "-F", "#{window_id}\t#{window_name}"], cwd)
    if reply.returncode != 0:
        return None
    found = []
    for line in reply.stdout.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        if "\t" not in line:
            return None
        window, name = line.split("\t", 1)
        if not WINDOW_ID.match(window):
            return None
        if name == title:
            found.append(window)
    return found[0] if len(found) == 1 else None


def find_pending(event: dict, cwd: Path) -> str | None:
    """A pending event (no handle saved): its terminal's handle or window id by unique title, or None.

    Found only from a fully well-formed exit-0 listing with exactly one exact title match;
    every other reply is unknown (None). A pending terminal is never judged closed.
    """
    title = _text(event.get("title"))
    if title is None:
        return None
    try:
        if "socket" in event:  # tmux pending; one with no saved socket is unknown at once.
            socket = _text(event.get("socket"))
            return _tmux_find(socket, title, cwd) if socket else None
        return _orca_find(title, cwd)
    except Exception:  # noqa: BLE001 - an unexpected failure tells nothing
        return None


def resolved(event: dict, cwd: Path) -> dict | None:
    """The event with its terminal identity: itself, a found pending one, or None when unknown."""
    if _text(event.get("handle")) or _text(event.get("window_id")):
        return event
    found = find_pending(event, cwd)
    if found is None:
        return None
    return {**event, "window_id": found} if "socket" in event else {**event, "handle": found}


def bring_forward(event: dict, cwd: Path) -> bool:
    """Show an open terminal (Orca terminal switch, tmux select-window); False if that failed."""
    try:
        if handle := _text(event.get("handle")):
            executable = shutil.which("orca")
            argv = [executable, "terminal", "switch", "--terminal", handle] if executable else None
        else:
            executable = shutil.which("tmux")
            socket, window = _text(event.get("socket")), _text(event.get("window_id"))
            argv = [executable, "-S", socket, "select-window", "-t", window] if executable and socket and window else None
        return argv is not None and _run(argv, cwd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def created_handle(stdout: str | None) -> str | None:
    """The handle Orca's ``terminal create --json`` reply names, from its ok envelope or flat object."""
    data = _json(stdout)
    if not isinstance(data, dict):
        return None
    if "ok" in data or "result" in data:
        result = data.get("result")
        if ("ok" in data and data["ok"] is not True) or not isinstance(result, dict):
            return None
        terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else {}
        candidates = (terminal.get("handle"), result.get("handle"), terminal.get("id"), result.get("id"))
    else:
        candidates = (data.get("handle"), data.get("id"), data.get("terminal"))
    return next((value for value in map(_text, candidates) if value), None)


def created_window(stdout: str | None) -> str | None:
    """The window id tmux ``new-window -P -F '#{window_id}'`` printed, when it is one."""
    window = (stdout or "").strip()
    return window if WINDOW_ID.match(window) else None

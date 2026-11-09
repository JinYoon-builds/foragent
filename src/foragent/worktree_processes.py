"""End the processes that still work inside a worktree before it is deleted (PM r1456).

A run's session can start detached test loops (`sh -c 'while ...; done | xargs fa test'`)
in their own group and session. When the runner dies they are reparented to pid 1, so no
recorded process tree reaches them, and their working directory keeps the worktree busy:
`git worktree remove` then fails with "Directory not empty" on every retry. The working
directory is the evidence that such a process belongs to the run that owned the worktree.
"""

from __future__ import annotations

import logging
from pathlib import Path
import signal
import time

import psutil

from foragent import processes


log = logging.getLogger(__name__)
WAIT_SECONDS = 3.0
POLL_SECONDS = 0.1


def _within(cwd: str | Path | None, root: Path) -> bool:
    """``cwd`` is ``root`` or below it, compared by path parts (issue-10 is not inside issue-1)."""
    if not cwd or not Path(cwd).is_absolute():
        return False
    try:
        Path(cwd).resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _cwd(row) -> str | Path | None:
    cwd = getattr(row, "cwd", None)
    if cwd is not None:
        return cwd
    try:
        return processes._cwd(psutil.Process(row.pid))
    except (psutil.Error, OSError):
        return None


def _inside(root: Path) -> dict[int, float]:
    """``{pid: start time}`` of every listed process whose working directory is under ``root``."""
    return {row.pid: row.start_time for row in processes.list_processes() if _within(_cwd(row), root)}


def _alive(pid: int, start: float) -> bool:
    try:
        current = processes.read_process_identity(pid)
    except (OSError, psutil.Error):
        return True  # Unknown counts as running: the SIGKILL stage reads it again first.
    return current is not None and current.start_time == start


def end_processes_inside(path: Path, *, wait_s: float = WAIT_SECONDS) -> list[tuple[int | None, str]]:
    """SIGTERM each process working inside ``path``, then SIGKILL the ones still there.

    Only processes whose working directory is ``path`` or below it are signalled, never fa's
    own lineage, group or session, and each signal is sent only after a fresh read shows the
    same process (``processes._signal_fresh``). Returns ``(pid or None, reason)`` for processes
    left running; the caller's delete then fails as before and reports it.
    """
    try:
        root = Path(path).resolve()
        targets = _inside(root)
        if not targets:
            return []
        rows, unreadable, _stable = processes._stable_scan()
        lineage = processes._own_lineage(rows, unreadable)
    except (OSError, psutil.Error) as exc:
        log.warning("Processes inside %s could not be read; none was ended: %s", path, exc)
        return [(None, str(exc))]
    left: dict[int | None, str] = {}
    allowed: dict[int, float] = {}
    for pid, start in targets.items():
        reason = processes.unverified_reason(pid, start, rows, unreadable, targets, lineage)
        if reason is None:
            allowed[pid] = start
        elif reason != processes.GONE:
            left[pid] = reason
    signalled: dict[int, float] = {}
    for pid, start in sorted(allowed.items(), key=lambda item: -item[1]):  # Children are born later.
        reason = processes._signal_fresh(pid, start, signal.SIGTERM)
        if reason is None:
            signalled[pid] = start
        else:
            left[pid] = reason
    for _ in range(int(wait_s / POLL_SECONDS)):
        if not any(_alive(pid, start) for pid, start in signalled.items()):
            break
        time.sleep(POLL_SECONDS)
    for pid, start in sorted(signalled.items(), key=lambda item: -item[1]):
        reason = processes._signal_fresh(pid, start, signal.SIGKILL)  # Gone: nothing is sent.
        if reason is not None:
            left[pid] = reason
    for pid, reason in left.items():
        log.warning("Process %s inside %s was not ended: %s", pid, path, reason)
    return list(left.items())

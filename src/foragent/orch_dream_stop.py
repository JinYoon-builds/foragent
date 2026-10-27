"""Liveness and stopping of a lane's dream process (#189, D95).

Only recorded ownership authorizes a stop, and only a confirmed absence lets
the tick finish a dream record; unknown liveness waits for a later tick.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def process_alive(pid: int | None, start_time: float | None, table) -> bool | None:
    """The one identity rule for a dream's recorded process: True alive, False gone, None unknown.

    A process is alive only when its pid exists with the recorded start time; a reused pid
    (another start time) is gone. A null start time (none could be read at launch, so no
    identity was recorded) means alive when the pid exists. An unreadable pid is unknown.
    """
    from foragent.scheduler_stop import _pid_exists

    if pid is None:
        return False
    unreadable = getattr(table, "unreadable", frozenset())
    if start_time is None:
        return pid in unreadable or _pid_exists(pid)
    for row in table:
        if row.pid == pid:
            return row.start_time == start_time
    return None if pid in unreadable else False


def _table():
    from foragent import processes

    try:
        return processes.list_processes()
    except (OSError, PermissionError):
        return None


def _starts(run) -> dict[int, float]:
    return {item["pid"]: item["start_time"] for item in run.process_identities}


def dream_liveness(run) -> bool | None:
    """Whether the dream's worker lives (``process_alive``); a record without a pid is unknown."""
    if run is None or run.status != "running":
        return False
    if run.pid is None or (table := _table()) is None:
        return None
    return process_alive(run.pid, _starts(run).get(run.pid), table)


def _remaining(run) -> bool | None:
    """Whether anything of the dream lives: worker, executor group leader, its members, recorded tree."""
    if (table := _table()) is None:
        return None
    starts = _starts(run)
    results = [process_alive(pid, starts.get(pid), table) for pid in {run.pid, run.executor_pgid, *starts}]
    group = run.executor_pgid
    if group is not None:
        leader = process_alive(group, starts.get(group), table)
        # Members of a group whose leader pid now belongs to another process are not ours.
        reused = leader is False and any(row.pid == group for row in table)
        results.append(not reused and any(row.pgid == group and row.pid != group for row in table))
    return True if True in results else None if None in results else False


def _maybe_ours(uid: int | None) -> bool:
    """A process of unknown owner, or of this user, may be the gate's worker."""
    return uid is None or uid == os.getuid()


def _worker_listed(lane: str) -> bool | None:
    """Whether a `gate run-dream <lane>` process is listed; None when that cannot be known.

    A process whose argv or identity is unreadable, and which may be ours, could be the
    worker: absence is confirmed only when every possibly-ours process was read.
    """
    from foragent import processes

    try:
        table = processes.list_processes()
    except (OSError, PermissionError):
        return None
    pattern = re.compile(rf"(?:^|\s)gate\s+run-dream\s+{re.escape(lane)}(?=\s|$)")
    if any(row.argv is not None and pattern.search(" ".join(row.argv)) for row in table):
        return True
    uids = getattr(table, "unreadable_uids", {})
    if any(row.argv is None and _maybe_ours(row.uid) for row in table) or any(
            _maybe_ours(uids.get(pid)) for pid in getattr(table, "unreadable", ())):
        return None
    return False


def _stop(ctx: TickContext, run, report: TickReport) -> bool:
    """Stop the dream's recorded tree (worker and executor); True only once all of it is confirmed gone.

    A stop that raises nothing is not proof: a worker pid without birth evidence is not
    in the recorded tree, so the killer may stop the executor and leave the worker alive.
    """
    from foragent import processes

    refusal = None
    try:
        with processes.recorded_tree(run.process_identities):
            for pid in (run.pid, run.executor_pgid):
                if pid is not None:
                    ctx.kill_runner(pid)
    except (ValueError, PermissionError, TimeoutError) as exc:
        refusal = exc
    if _remaining(run) is False:
        return True  # Nothing of the dream is left, confirmed by one full read (process_alive).
    reason = f"stop refused: {refusal}" if refusal is not None else "still running after the stop"
    report.actions.append(f"lane {run.lane} dream {' '.join(reason.split())[:200]}")
    return False

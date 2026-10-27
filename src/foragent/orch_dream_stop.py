"""Liveness and stopping of a lane's dream process (#189, D95).

Only recorded ownership authorizes a stop, and only a confirmed absence lets
the tick finish a dream record; unknown liveness waits for a later tick.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def dream_liveness(run) -> bool | None:
    """True when the dream's process is confirmed alive, False when confirmed gone, None when unknown.

    An unreadable process, an unavailable listing or a record without a pid never proves death.
    """
    from foragent import processes
    from foragent.scheduler_stop import _pid_exists

    if run is None or run.status != "running":
        return False
    if run.pid is None:
        return None
    try:
        table = processes.list_processes()
    except (OSError, PermissionError):
        return None
    unreadable = getattr(table, "unreadable", frozenset())
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    if run.pid in identities:
        if any(row.pid == run.pid and row.start_time == identities[run.pid] for row in table):
            return True
        return None if run.pid in unreadable else False
    if run.pid in unreadable:
        return None
    return _pid_exists(run.pid)


def _worker_listed(lane: str) -> bool | None:
    """Whether a `gate run-dream <lane>` process is listed; None when the listing is unavailable."""
    from foragent import processes

    try:
        table = processes.list_processes()
    except (OSError, PermissionError):
        return None
    pattern = re.compile(rf"(?:^|\s)gate\s+run-dream\s+{re.escape(lane)}(?=\s|$)")
    return any(row.argv is not None and pattern.search(" ".join(row.argv)) for row in table)


def _stop(ctx: TickContext, run, report: TickReport) -> bool:
    """Stop the dream's recorded tree (worker and executor); False when the stop is refused."""
    from foragent import processes
    from foragent.scheduler_stop import _run_processes_exist

    try:
        with processes.recorded_tree(run.process_identities):
            for pid in (run.pid, run.executor_pgid):
                if pid is not None:
                    ctx.kill_runner(pid)
        return True
    except (ValueError, PermissionError, TimeoutError) as exc:
        try:
            if not _run_processes_exist(run):
                return True  # Nothing of the dream is left to stop.
        except PermissionError:
            pass  # An unreadable table cannot prove death.
        report.actions.append(f"lane {run.lane} dream stop refused: {' '.join(str(exc).split())[:200]}")
        return False

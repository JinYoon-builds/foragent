"""Liveness and stopping of a lane's dream process (#189, D95).

Only recorded ownership authorizes a stop, and only a confirmed absence lets
the tick finish a dream record; unknown liveness waits for a later tick.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
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
    """Whether anything of the dream lives: worker, executor group leader and the recorded tree.

    Only recorded identities count. A process-group number alone proves nothing: after the
    group's processes exit, the number can name an unrelated group, whose members are not ours.
    """
    if (table := _table()) is None:
        return None
    starts = _starts(run)
    results = [process_alive(pid, starts.get(pid), table) for pid in {run.pid, run.executor_pgid, *starts}]
    return True if True in results else None if None in results else False


def _maybe_ours(uid: int | None) -> bool:
    """A process of unknown owner, or of this user, may be the gate's worker."""
    return uid is None or uid == os.getuid()


def _worker_argv(lane: str, repo_root) -> list[str]:
    """The launcher's exact worker command after the interpreter (launcher.spawn_runner)."""
    return ["-m", "foragent.cli", "gate", "run-dream", lane, "--repo-root", str(Path(repo_root).resolve())]


def _workers_listed(lane: str, repo_root) -> list | None:
    """This repository's dream workers for the lane; None when that cannot be known.

    A worker is matched by its whole argv, element by element, as the launcher builds it
    (any interpreter, then exactly ``-m foragent.cli gate run-dream <lane> --repo-root <root>``):
    a search tool or shell whose arguments merely contain that text is not a worker, and
    another repository's worker of a lane with the same name has another root. A process
    whose argv or identity is unreadable, and which may be ours, could be the worker, so
    an empty list is returned only when every possibly-ours process was read. A listing
    is evidence of life only; `adoptable` decides whether the tick may stop it.
    """
    from foragent import processes

    try:
        table = processes.list_processes()
    except (OSError, PermissionError):
        return None
    expected = _worker_argv(lane, repo_root)
    found = [row for row in table if row.argv is not None and list(row.argv[1:]) == expected
             and _maybe_ours(row.uid)]
    if found:
        return found
    uids = getattr(table, "unreadable_uids", {})
    if any(row.argv is None and _maybe_ours(row.uid) for row in table) or any(
            _maybe_ours(uids.get(pid)) for pid in getattr(table, "unreadable", ())):
        return None
    return []


def adoptable(row, run, grace_seconds: float) -> bool:
    """Whether a listed worker is this record's own process, strongly enough to stop it.

    The launcher starts the worker as a new session right after it writes the record, so
    the worker is ours (same uid), leads its own session and group, and was born between
    the record's start and the launch grace. A listing that fails any of these is only
    evidence of life, never signal ownership.
    """
    started = datetime.fromisoformat(run.started_at).timestamp()
    return (row.uid == os.getuid() and row.pid == row.pgid == row.sid
            and started - 1 <= row.start_time <= started + grace_seconds)


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

"""The gate tick's side of a talk (D55 addendum, D95): the only writer of a run's talk state.

A run's current talk is derived from the orch events journal (``state_talk``). Each
tick probes it with the shared liveness function (``lane_talk_probe``). Only closed
writes: one ``talk-closed`` event and the paused time credited once through the
union helper. Open or unknown keep the talk, and stall and runtime checks stay
paused. A newer ``talk-opened`` event supersedes an older one, which closes at the
newer event's time. Opening terminals and sending messages live in ``lane_talk_open``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from foragent import lane_talk_probe as probe
from foragent.state import StateStore
from foragent.state_records import Run


def check_talking(store: StateStore, run: Run, root: Path, now: datetime) -> tuple[Run, bool]:
    """Tick step for a talking run: ``(run, still_talking)``; only a confirmed closure ends it."""
    current, superseded = store.talk_state(run)
    for event, newer_at in superseded:
        store.close_talk(run.number, event, datetime.fromisoformat(newer_at))
    if current is None:
        return store.get_run(run.number) or run, False
    cwd = Path(run.worktree) if Path(run.worktree).is_dir() else root
    target = probe.resolved(current, cwd)  # A pending title found now serves this probe only.
    if target is None or probe.liveness(target, cwd) != probe.CLOSED:
        return store.get_run(run.number) or run, True
    store.close_talk(run.number, current, now)
    return store.get_run(run.number) or run, False


def talking_now(store: StateStore, number: int) -> bool:
    """Read again right before a stop for time: an open (or just opened) talk pauses it."""
    run = store.get_run(number)
    return run is not None and run.talking


def stall_since(store: StateStore, run: Run) -> datetime:
    """The stall clock's start: the later of the last progress and the latest talk closing."""
    times = [value for value in (run.last_progress_at or run.started_at, store.talk_closed_at(run)) if value]
    return max(datetime.fromisoformat(value) for value in times)

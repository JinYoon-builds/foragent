"""The gate tick credits machine sleep to running runs; nothing else writes it (#415).

Each full tick reads ``capacity.slept_seconds`` from the previous tick's time,
stored per repository in ``<state>/sleep-baseline.json``, under the state lock.
The increment, clamped to the wall time since that tick, is added to
``slept_seconds`` and ``slept_since_progress`` of every running run that is not
waiting. A waiting run's wall-timed wait already covers the time. A run that
started after the previous tick gets nothing from this one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json

from foragent import capacity
from foragent.state import Run, StateStore

BASELINE_FILE = "sleep-baseline.json"


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _waiting(run: Run, now: datetime) -> bool:
    """Any wait phase (overlap, test slot, baseline, limit waits) or a future waiting_until."""
    until = _time(run.waiting_until)
    return (run.phase or "").endswith("-wait") or (until is not None and until > now)


def credit(store: StateStore, now: datetime) -> float:
    """Credit the sleep since the previous tick and store this tick's time; return the increment."""
    now = now.astimezone(UTC)
    path = store.root / BASELINE_FILE
    with store._state_lock():
        try:
            previous = _time(json.loads(path.read_text(encoding="utf-8")).get("at"))
        except (OSError, ValueError, AttributeError):
            previous = None
        increment = 0.0
        if previous is not None and previous <= now:  # Without one, or after the clock moved back: 0.
            slept = capacity.slept_seconds(previous)
            increment = min(max(0.0, slept), (now - previous).total_seconds())
            for run in store.active_runs():
                started = _time(run.started_at)
                if started is None or started > previous:
                    continue
                progress = _time(run.last_progress_at)
                since_progress = 0.0 if progress is not None and progress > previous else run.slept_since_progress
                slept_run = run.slept_seconds
                if not _waiting(run, now):
                    slept_run, since_progress = slept_run + increment, since_progress + increment
                if (slept_run, since_progress) != (run.slept_seconds, run.slept_since_progress):
                    store._write_run(replace(run, slept_seconds=slept_run, slept_since_progress=since_progress))
        capacity.slept_seconds(now)  # Records the sensor's clock baseline at this tick's time.
        store._write_json(path, {"at": now.isoformat()})
    return increment

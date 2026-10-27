"""Phase journal entries (#224 schema) and the one rule for repeated work."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

from foragent.state_records import Run


def _load() -> float:
    try:
        return float(os.getloadavg()[0])
    except OSError:
        return 0.0


def work_event(run: Run) -> tuple[str, str | None] | None:
    """(event_id, repeat_of) of the run's current work instance, or None without a work identity.

    Every segment of one instance (split by nested waits) shares its event_id; only a later
    instance of the same work repeats the first. Open and closed phases use this one rule.
    """
    if not run.phase_work_id:
        return None
    instance = run.phase_work_started_at or run.phase_started_at
    event_id = f"{run.number}:{run.phase_work_id}:{instance}"
    first = run.work_events.get(run.phase_work_id)
    return event_id, (first if first is not None and first != event_id else None)


def close_phase(root: Path, run: Run, closed_at: datetime) -> dict[str, Any]:
    """Journal the ended phase: its own seconds and the 1-minute load.

    Callers hold the state lock and call this as each transition happens, so readers see
    it at once. Work with an identity also gets event_id/work_id, and repeat_of when the
    same work already ended once. Returns the run fields this closing changes.
    """
    if not run.phase or not run.phase_started_at:
        return {}
    opened = datetime.fromisoformat(run.phase_started_at)
    event = {"at": closed_at.astimezone(UTC).isoformat(), "issue": run.number, "kind": "phase",
             "phase": run.phase, "seconds": max(0.0, (closed_at - opened).total_seconds()),
             "load": _load(),
             # Identifies the interval, so a close journaled again (the run record failed
             # to persist and the phase closes once more) replaces rather than adds time.
             "phase_started_at": run.phase_started_at}
    changes: dict[str, Any] = {}
    if (work := work_event(run)) is not None:
        event_id, repeat_of = work
        event.update(event_id=event_id, work_id=run.phase_work_id)
        if repeat_of is not None:
            event["repeat_of"] = repeat_of
        elif run.phase_work_id not in run.work_events:
            changes["work_events"] = {**run.work_events, run.phase_work_id: event_id}
    with (root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return changes

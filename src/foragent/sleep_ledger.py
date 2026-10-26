"""Machine-wide sleep ledger: sleep is neither run time nor stall time (#415).

Each gate tick records the sleep since the previous tick's baseline, so a run's
credited sleep is the ledger total since its start. A tick that sees more than
WAKE_THRESHOLD of sleep opens a machine-wide wake grace: stalls wait until a run
shows new activity or the grace ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

from foragent import capacity
from foragent.i18n import text
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

LEDGER_FILE = "sleep-ledger.json"
WAKE_THRESHOLD = timedelta(seconds=60)
WAKE_GRACE = timedelta(minutes=5)


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Ledger:
    entries: list[dict[str, Any]] = field(default_factory=list)
    wake_at: datetime | None = None
    grace_until: datetime | None = None

    def credit(self, since: str, now: datetime) -> float:
        """The least ledger sleep that must lie after ``since``; awake time is never credited.

        Waits are timed with clocks that pause in sleep, so no sleep is in waited_seconds.
        """
        start = _time(since)
        return 0.0 if start is None else capacity.sleep_within(self.entries, start, now)

    def defers_stall(self, run: Run, now: datetime) -> bool:
        """True in the wake grace until the run shows activity after the wake."""
        if self.wake_at is None or self.grace_until is None or now >= self.grace_until:
            return False
        seen = [moment for moment in (_time(run.last_progress_at), _time(run.last_activity_at)) if moment]
        return not any(moment > self.wake_at for moment in seen)


def _load(raw: dict[str, Any]) -> tuple[datetime | None, Ledger]:
    entries = []
    for entry in raw.get("entries", []):
        start, end = _time(entry.get("start")), _time(entry.get("end"))
        if start is None or end is None or not isinstance(entry.get("seconds"), (int, float)):
            continue
        entries.append({"start": start, "end": end, "seconds": float(entry["seconds"])})
    return _time(raw.get("baseline_at")), Ledger(entries, _time(raw.get("wake_at")), _time(raw.get("grace_until")))


def _write(baseline: datetime, ledger: Ledger) -> None:
    root = capacity.machine_root()
    value = {
        "baseline_at": baseline.isoformat(),
        "entries": [{**entry, "start": entry["start"].isoformat(), "end": entry["end"].isoformat()}
                    for entry in ledger.entries],
        "wake_at": ledger.wake_at.isoformat() if ledger.wake_at else None,
        "grace_until": ledger.grace_until.isoformat() if ledger.grace_until else None,
    }
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{LEDGER_FILE}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
    os.replace(temporary, root / LEDGER_FILE)


def observe(ctx: TickContext, report: TickReport) -> Ledger:
    """Append this tick's sleep, open a wake grace after a long sleep, store the new baseline."""
    root = capacity.machine_root()
    with capacity._flock(root / f"{LEDGER_FILE}.lock"):
        now = ctx.now().astimezone(UTC)  # Inside the lock: a tick that waited is never older.
        try:
            raw = json.loads((root / LEDGER_FILE).read_text(encoding="utf-8"))
            baseline, ledger = _load(raw if isinstance(raw, dict) else {})
        except (OSError, ValueError, AttributeError):
            baseline, ledger = None, Ledger()
        if baseline is not None and baseline > now:
            # The clock moved back: facts dated after now cannot be placed; earlier ones stay.
            ledger.entries = [entry for entry in ledger.entries if entry["end"] <= now]
            if ledger.wake_at is not None and ledger.wake_at > now:
                ledger.wake_at = ledger.grace_until = None
            baseline = None
        if baseline is not None and (slept := capacity.slept_seconds(baseline)) > 0:
            slept = min(slept, (now - baseline).total_seconds())
            ledger.entries.append({"start": baseline, "end": now, "seconds": slept})
            if slept > WAKE_THRESHOLD.total_seconds():
                # The earliest the machine can have woken; activity after it is post-wake.
                ledger.wake_at, ledger.grace_until = baseline + timedelta(seconds=slept), now + WAKE_GRACE
                hours, minutes = divmod(round(slept / 60), 60)
                report.actions.append(text(
                    "sleep_wake", ctx.config.language or "en", hours=hours, minutes=minutes,
                    until=ledger.grace_until.astimezone().strftime("%H:%M"),
                ))
        capacity.slept_seconds(now)  # Records the clock baseline for the next tick.
        oldest = capacity.oldest_running_start({ctx.store.root})  # Every repository's runs.
        ledger.entries = [entry for entry in ledger.entries if oldest is not None and entry["end"] >= oldest]
        _write(now, ledger)
    return ledger

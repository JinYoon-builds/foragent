"""Machine-wide sleep ledger: sleep is neither run time nor stall time (#415).

Each gate tick records the sleep since the previous tick's baseline, so a run's
credited sleep is the ledger total since its start. Sleep is placed where the
sensor puts it, so each run's own window is intersected with the sleep itself. A tick that sees more than
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
# A window whose sensor difference is below this holds no sleep (float noise only).
SLEEP_EPSILON = 1e-6
WAKE_THRESHOLD = timedelta(seconds=60)
WAKE_GRACE = timedelta(minutes=5)
# Sleep is placed to this resolution; a coarser boundary keeps only its least sleep.
PLACEMENT_SECONDS = 1.0
# Placement repeats while a sleep during it changes what the sensor reports.
PLACEMENT_TRIES = 3


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
    # The moment this ledger was observed; runs are judged at it, since its sleep ends there.
    at: datetime | None = None

    def credit(self, since: str, now: datetime) -> float:
        """Ledger sleep inside this run's own window [since, now]; awake time is never credited.

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


def _place(start: datetime, end: datetime, sleep_after) -> list[dict[str, Any]]:
    """Find where in [start, end] the sleep lies from ``sleep_after(t)``, the sleep after t.

    The sleep inside [a, b] is sleep_after(a) - sleep_after(b); halving a window
    until it is wholly asleep or awake places each sleep to PLACEMENT_SECONDS.
    """
    found: list[dict[str, Any]] = []

    def split(a: datetime, after_a: float, b: datetime, after_b: float) -> None:
        inside, span = after_a - after_b, (b - a).total_seconds()
        if inside < SLEEP_EPSILON:
            return
        # Every fragment keeps its exact seconds, so the parts sum to the window's sleep.
        if inside >= span - SLEEP_EPSILON or span <= PLACEMENT_SECONDS:
            found.append({"start": a, "end": b, "seconds": min(inside, span)})
            return
        middle = a + (b - a) / 2
        after_middle = sleep_after(middle)
        split(a, after_a, middle, after_middle)
        split(middle, after_middle, b, after_b)

    split(start, sleep_after(start), end, sleep_after(end))
    merged: list[dict[str, Any]] = []
    for item in found:
        if merged and merged[-1]["end"] == item["start"]:
            merged[-1] = {**merged[-1], "end": item["end"], "seconds": merged[-1]["seconds"] + item["seconds"]}
        else:
            merged.append(item)
    return merged


def _placed_sleep(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], float]:
    """Placement and the window's total sleep from consistent readings.

    Retried while a new sleep moves the sensor. The total is the sensor's own
    reading of the window, not a sum of placed parts.
    """
    for _ in range(PLACEMENT_TRIES):
        before = capacity.slept_seconds(start) - capacity.slept_seconds(end)
        placed = _place(start, end, capacity.slept_seconds)
        after = capacity.slept_seconds(start) - capacity.slept_seconds(end)
        if abs(after - before) < SLEEP_EPSILON and abs(sum(item["seconds"] for item in placed) - after) < 1e-3:
            return placed, after
    return placed, sum(item["seconds"] for item in placed)


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
        if baseline is not None and capacity.slept_seconds(baseline) > 0:
            placed, slept = _placed_sleep(baseline, now)
            ledger.entries += placed
            if placed and slept > WAKE_THRESHOLD.total_seconds():
                # A measured wake: the kernel's wake time, or the read that found the
                # sleep; never earlier than the real wake, so pre-sleep activity stays old.
                measured = capacity.latest_wake(baseline)
                wake = max(placed[-1]["end"], measured) if measured else placed[-1]["end"]
                ledger.wake_at, ledger.grace_until = min(wake, now), now + WAKE_GRACE
                hours, minutes = divmod(round(slept / 60), 60)
                report.actions.append(text(
                    "sleep_wake", ctx.config.language or "en", hours=hours, minutes=minutes,
                    until=ledger.grace_until.astimezone().strftime("%H:%M"),
                ))
        capacity.slept_seconds(now)  # Records the clock baseline for the next tick.
        oldest = capacity.oldest_running_start({ctx.store.root})  # Every repository's runs.
        ledger.entries = [entry for entry in ledger.entries if oldest is not None and entry["end"] >= oldest]
        _write(now, ledger)
    ledger.at = now
    return ledger

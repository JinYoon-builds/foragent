"""Machine-wide wake grace: sleep is neither run time nor stall time (#415).

The sleep itself is kept by the sensor's machine-wide history
(``capacity.slept_seconds``); every run window reads it in one consistent
snapshot. This ledger keeps what a tick adds on top: the previous tick's
baseline and the wake grace. A tick that sees more than WAKE_THRESHOLD of sleep
since that baseline opens a machine-wide grace: stalls wait until a run shows
new activity after the wake or the grace ends.
"""

from __future__ import annotations

from dataclasses import dataclass
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
# A sensor reading below this is float noise, not sleep.
SLEEP_EPSILON = 1e-6
WAKE_THRESHOLD = timedelta(seconds=60)
WAKE_GRACE = timedelta(minutes=5)
# An observation repeats while its final probe sees sleep after its own end.
OBSERVE_TRIES = 3


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Ledger:
    wake_at: datetime | None = None
    grace_until: datetime | None = None
    # The moment of this observation: no sleep followed it when it was taken.
    at: datetime | None = None

    def defers_stall(self, run: Run, now: datetime) -> bool:
        """True in the wake grace until the run shows activity after the wake."""
        if self.wake_at is None or self.grace_until is None or now >= self.grace_until:
            return False
        seen = [moment for moment in (_time(run.last_progress_at), _time(run.last_activity_at)) if moment]
        # An event stamped at the wake time itself already proves the machine awake.
        return not any(moment >= self.wake_at for moment in seen)


def _write(baseline: datetime, ledger: Ledger) -> None:
    root = capacity.machine_root()
    value = {
        "baseline_at": baseline.isoformat(),
        "wake_at": ledger.wake_at.isoformat() if ledger.wake_at else None,
        "grace_until": ledger.grace_until.isoformat() if ledger.grace_until else None,
    }
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{LEDGER_FILE}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
    os.replace(temporary, root / LEDGER_FILE)


def _baseline(raw: dict[str, Any], ctx: TickContext, now: datetime) -> datetime:
    """The previous tick's baseline; without a usable one, the oldest running run's start.

    A missing ledger or a clock that moved back would otherwise drop sleep the
    sensor already holds for runs that are still running.
    """
    baseline = _time(raw.get("baseline_at"))
    if baseline is not None and baseline <= now:
        return baseline
    oldest = capacity.oldest_running_start({ctx.store.root})
    return oldest if oldest is not None and oldest <= now else now


def current(ctx: TickContext, report: TickReport, ledger: Ledger) -> Ledger:
    """This ledger, or a new observation if the machine slept after it was taken."""
    if ledger.at is not None and capacity.slept_seconds(ledger.at) < SLEEP_EPSILON:
        return ledger
    return observe(ctx, report)


def observe(ctx: TickContext, report: TickReport) -> Ledger:
    """Open a wake grace after a long sleep since the baseline and store the new baseline."""
    root = capacity.machine_root()
    with capacity._flock(root / f"{LEDGER_FILE}.lock"):
        now = ctx.now().astimezone(UTC)  # Inside the lock: a tick that waited is never older.
        try:
            raw = json.loads((root / LEDGER_FILE).read_text(encoding="utf-8"))
            raw = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            raw = {}
        ledger = Ledger(_time(raw.get("wake_at")), _time(raw.get("grace_until")))
        if ledger.wake_at is not None and ledger.wake_at > now:
            ledger = Ledger()  # The clock moved back past the recorded wake.
        baseline = _baseline(raw, ctx, now)
        # The window's sleep and its end are one snapshot. A final probe that sees
        # sleep after the end means a suspend came in between: observe again.
        for _ in range(OBSERVE_TRIES):
            total, now = capacity.slept_until(baseline, lambda: ctx.now().astimezone(UTC))
            if capacity.slept_seconds(now) < SLEEP_EPSILON:  # Also samples the next baseline.
                break
        if total > WAKE_THRESHOLD.total_seconds():
            # A measured wake: the kernel's wake time or the read that found the sleep,
            # else this tick. Never earlier than the real wake, so pre-sleep activity stays old.
            measured = capacity.latest_wake(baseline)
            ledger.wake_at = min(measured, now) if measured else now
            ledger.grace_until = now + WAKE_GRACE
            hours, minutes = divmod(round(total / 60), 60)
            report.actions.append(text(
                "sleep_wake", ctx.config.language or "en", hours=hours, minutes=minutes,
                until=ledger.grace_until.astimezone().strftime("%H:%M"),
            ))
        _write(now, ledger)
    ledger.at = now
    return ledger

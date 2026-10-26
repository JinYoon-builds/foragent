"""Machine-wide sleep ledger: sleep is neither run time nor stall time (#415).

The ledger is an append-only list in the machine data root. Each reading, under
the ledger's lock, appends the sleep since the previous reading as one record
``(since, until, seconds)``: ``seconds`` of sleep lie somewhere in [since,
until]. Readings partition time, and the last record doubles as the checkpoint,
so a sleep is recorded once even if the state write after it is lost.

An awake event (a run's start, its progress, a wait's start) is stamped with a
reading's own moment (``mark``), so every event is a record boundary: a record
ends at or before it or starts at or after it. A consumer credits its window
[start, now] with the records after ``start``, and a direct sensor read from
``start`` in the same snapshot; both are lower bounds of the window's sleep and
the larger one is credited. A start that is no reading moment (an older record)
gets only the sleep that cannot fit between a record's ``since`` and the start.

The gate's tick, and only the tick, opens the wake grace: when the sleep since
its previous tick exceeds WAKE_THRESHOLD, stalls wait WAKE_GRACE from that tick
until a run shows an event at or after the end of the sleep that opened it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
import tempfile
from typing import Any

from foragent import capacity
from foragent.i18n import text

LEDGER_FILE = "sleep-ledger.jsonl"
STATE_FILE = "sleep-ledger-state.json"
# Sleep below this is clock rounding, not sleep.
SLEEP_MIN_SECONDS = 1.0
WAKE_THRESHOLD = timedelta(seconds=60)
WAKE_GRACE = timedelta(minutes=5)
# A reading repeats while a suspend moves the sensor between its two reads.
READ_TRIES = 5

Record = tuple[datetime, datetime, float]


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _after(record: Record, start: datetime) -> float:
    """The least sleep of ``record`` that lies after the awake event ``start``."""
    since, until, seconds = record
    if start >= until:
        return 0.0
    return max(0.0, seconds - max(0.0, (start - since).total_seconds()))


@dataclass(frozen=True)
class Snapshot:
    """The ledger, the direct sensor reads and the reading's time, taken together under the lock."""

    records: tuple[Record, ...]
    now: datetime
    sensor: dict[datetime, float] = field(default_factory=dict)
    grace_until: datetime | None = None
    grace_records: tuple[Record, ...] = ()

    def credit(self, start: datetime) -> float:
        """Sleep inside [start, now]; ``start`` is an awake event."""
        ledger = sum(_after(record, start) for record in self.records)
        return max(ledger, self.sensor.get(start, 0.0))

    def defers_stall(self, events: Iterable[datetime]) -> bool:
        """In the tick's wake grace, until an event after the sleep that opened it."""
        if self.grace_until is None or self.now >= self.grace_until or not self.grace_records:
            return False
        # Event times are ledger reading moments (``mark``), so an event at or after the
        # record's end is after the wake; one inside the record may precede the sleep.
        latest = max(self.grace_records, key=lambda record: record[1])
        return not any(event >= latest[1] for event in events)


def _load(path) -> list[Record]:
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            value = json.loads(line)
            since, until = _time(value.get("since")), _time(value.get("until"))
            seconds = float(value["seconds"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # A torn last line from a crash.
        if since is not None and until is not None and since < until:
            records.append((since, until, seconds))
    return records


def _line(record: Record) -> str:
    since, until, seconds = record
    return json.dumps({"since": since.isoformat(), "until": until.isoformat(), "seconds": seconds}) + "\n"


def _append(path, line: str) -> None:
    """Append one record; a torn last line from a crash is closed first, so it stays alone."""
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        stream.write(line.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def mark(now: Callable[[], datetime]) -> datetime:
    """An awake event's time: a ledger reading's own moment, so the event is a record boundary."""
    return read(now).now


def _replace(path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.replace(temporary, path)


def _measure(points: list[datetime], now: Callable[[], datetime]) -> tuple[list[float], datetime]:
    """Sensor reads from each point and a moment, between two equal sets of reads.

    The read at the moment records the sensor's baseline for the next reading,
    inside the same bracket, so a suspend can never fall between two readings.
    """
    for _ in range(READ_TRIES):
        first = [capacity.slept_seconds(point) for point in points]
        moment = now()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        moment = moment.astimezone(UTC)
        capacity.slept_seconds(moment)
        second = [capacity.slept_seconds(point) for point in points]
        # Equal both ways: macOS reports only its latest sleep, so a new one can also lower a read.
        if all(abs(b - a) < SLEEP_MIN_SECONDS for a, b in zip(first, second)):
            return first, moment
    return second, moment


def read(
    now: Callable[[], datetime], starts: Iterable[datetime] = (), *, tick: bool = False,
    report: list[str] | None = None, language: str = "en",
) -> Snapshot:
    """Append the sleep since the previous reading and return one snapshot.

    ``starts`` are window starts to read the sensor from directly. A gate tick
    passes ``tick=True``: it opens the wake grace and reports one wake line.
    """
    root = capacity.machine_root()
    path, state_path = root / LEDGER_FILE, root / STATE_FILE
    starts = list(starts)
    with capacity._flock(root / f"{LEDGER_FILE}.lock"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            state = {}
        oldest = capacity.oldest_running_start()
        records = _load(path)
        # The record is the checkpoint: one appended before a lost state write is not read again.
        previous = max((moment for moment in (_time(state.get("read_at")), records[-1][1] if records else None)
                        if moment is not None), default=None)
        # Without a usable previous reading, read from the oldest running start.
        since = previous if previous is not None and (oldest is None or previous >= oldest) else oldest
        points = [since] if since is not None else []
        values, moment = _measure(points + starts, now)
        if since is not None and since <= moment:
            slept = min(values[0], (moment - since).total_seconds())
            if slept >= SLEEP_MIN_SECONDS:
                record = (since, moment, slept)
                _append(path, _line(record))
                records.append(record)
        if oldest is not None and records and records[0][1] <= oldest:
            # Only history no running run's window can reach; a reader that sees none prunes nothing.
            records = [record for record in records if record[1] > oldest]
            _replace(path, "".join(_line(record) for record in records))
        current = tuple(record for record in records if record[1] <= moment)  # A clock moved back.
        sensor = dict(zip(starts, values[len(points):]))
        state["read_at"] = moment.isoformat()
        if tick:
            _tick(state, current, moment, oldest, report, language)
        _replace(state_path, json.dumps(state))
    grace_since, grace_until = _time(state.get("grace_since")), _time(state.get("grace_until"))
    grace = tuple(record for record in current
                  if grace_since is not None and record[0] >= grace_since and record[1] <= moment)
    return Snapshot(current, moment, sensor, grace_until, grace)


def _tick(state: dict[str, Any], records: tuple[Record, ...], moment: datetime,
          oldest: datetime | None, report: list[str] | None, language: str) -> None:
    """Open the wake grace when the sleep since the previous tick exceeds the threshold."""
    previous = _time(state.get("tick_at"))
    if previous is None or previous > moment:
        previous = oldest if oldest is not None and oldest <= moment else moment
    window = [record for record in records if record[0] >= previous]
    slept = sum(record[2] for record in window)
    if slept > WAKE_THRESHOLD.total_seconds():
        state["grace_since"], state["grace_until"] = previous.isoformat(), (moment + WAKE_GRACE).isoformat()
        if report is not None:
            hours, minutes = divmod(round(slept / 60), 60)
            report.append(text("sleep_wake", language or "en", hours=hours, minutes=minutes,
                               until=(moment + WAKE_GRACE).astimezone().strftime("%H:%M")))
    state["tick_at"] = moment.isoformat()

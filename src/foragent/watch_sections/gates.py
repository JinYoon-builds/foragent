"""The other gates installed on this machine, last in the frame.

Every gate fits on one line when the width allows; otherwise the gates wrap
between entries, so no gate is ever clipped away by the frame width.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 70


def _tick(timestamp: str | None, now: datetime, language: str) -> str:
    """ISO time to the minute in UTC, then the relative age; 'never' without a tick."""
    from foragent import gates

    if timestamp is None:
        return gates.age(None, now, language)
    moment = datetime.fromisoformat(timestamp).astimezone(UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%MZ')} ({gates.age(timestamp, now, language)})"


def _entry(row: dict[str, Any], now: datetime, language: str) -> str:
    from foragent import watch

    entry = f"{row['repo']} {_tick(row['last_tick'], now, language)}"
    if row["stale"]:
        entry += " ⚠"
    if row["running_runs"]:
        entry += " " + watch.text("gates_lane_running", language, count=row["running_runs"])
    return entry


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    others = [row for row in snapshot.status.get("installed_gates") or [] if not row.get("current")]
    if not others:
        return []
    now = context.get("now") or snapshot.captured_at
    width = context.get("width") or 120
    heading = watch.text("gates_lane_heading", config.language)
    indent = " " * watch._cells(heading + " ")
    lines: list[str] = [heading]
    for entry in (_entry(row, now, config.language) for row in others):
        joined = f"{lines[-1]} · {entry}" if lines[-1] != heading else f"{heading} {entry}"
        if watch._cells(joined) <= width or lines[-1] == heading:
            lines[-1] = joined
        else:
            lines.append(indent + entry)
    # An entry wider than the frame still wraps word by word instead of being clipped.
    return [wrapped for line in lines for wrapped in (
        [watch._line(line)] if watch._cells(line) <= width else watch._wrap(line, width))]

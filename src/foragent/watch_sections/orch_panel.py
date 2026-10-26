"""The orchestrator note inside the lanes section's 🧭 box: now, why, next, where and the inbox count.

This module adds no section of its own; the lanes section places these lines in its box.
"""

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.state import StateStore
from foragent.watch_cells import Line

ORDER = 35
# `fa inspect` writes this many-items count; an older one falls back to the status summary.
COUNT_FILE = "inspect-count.json"
COUNT_FRESH = timedelta(minutes=10)


def render(snapshot: Any, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    return []  # The lanes section draws these lines inside its orchestrator box.


def write_count(store: StateStore, count: int, now: datetime) -> None:
    """Record how many items `fa inspect` found, for the lane view.

    Inspecting never creates the state folder; without one there is no view to inform.
    """
    if not store.root.is_dir():
        return
    store._write_json(store.root / COUNT_FILE, {"count": count, "at": now.isoformat()})


def _seconds(value: Any, now: datetime) -> float | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    return max(0.0, (now - at).total_seconds())


def collect(store: StateStore, now: datetime) -> dict[str, Any]:
    """The note, its age in seconds at ``now``, and a fresh inspect count (None when missing or old)."""
    found: dict[str, Any] = {"note": None, "note_age": None, "inbox": None}
    note = store.orch_note()
    if note is not None and isinstance(note.get("now"), str) and note["now"].strip():
        found["note"], found["note_age"] = note, _seconds(note.get("at"), now)
    try:
        record = json.loads((store.root / COUNT_FILE).read_text(encoding="utf-8"))
        age = _seconds(record.get("at"), now)
        if age is not None and age <= COUNT_FRESH.total_seconds() and type(record.get("count")) is int:
            found["inbox"] = record["count"]
    except (OSError, ValueError, AttributeError):
        pass  # No usable count: the status summary stands in.
    return found


def age_text(seconds: float, language: str) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return text("orch_panel_just_now", language)
    if minutes < 60:
        return text("orch_panel_minutes", language, count=minutes)
    if minutes < 24 * 60:
        return text("orch_panel_hours", language, count=minutes // 60)
    return text("orch_panel_days", language, count=minutes // (24 * 60))


def where_text(where: Any, language: str) -> str | None:
    if not isinstance(where, dict):
        return None
    kind = where.get("kind")
    if kind == "orca":
        title = where.get("title")
        return text("orch_panel_orca", language) + (f" {title}" if title else "")
    if kind == "tmux" and where.get("pane"):
        return text("orch_panel_tmux", language, pane=where["pane"])
    if kind == "terminal":
        return text("orch_panel_terminal", language)
    return None


def cells(value: str) -> int:
    from foragent import watch

    return sum(watch._cells(char) for char in value)


def _labelled(label: str, value: str, width: int, style: str = "") -> list[Line]:
    """The value after its label, continuing on lines indented under the value."""
    from foragent import watch

    lead = f"{label}: " if label else ""
    indent = sum(watch._cells(char) for char in lead)
    if width - indent < 8:
        return watch._wrap(lead + value, max(1, width), style)  # Too narrow to indent.
    pieces = watch._wrap(value, max(1, width - indent), style)
    lines = [[(lead, style), *pieces[0]]]
    lines += [[(" " * indent, style), *piece] for piece in pieces[1:]]
    return lines


def lines(panel: dict[str, Any], waiting_human: Any, language: str, width: int,
          age: float, fresh: bool) -> list[Line]:
    """Note lines in order now, why, next, where, then the count for people."""
    from foragent import watch

    found: list[Line] = []
    note = panel.get("note")
    if note is not None:
        label = text("orch_panel_now", language)
        found += _labelled(label, note["now"], width)
        tail = [age_text(panel["note_age"] + age, language)] if panel.get("note_age") is not None else []
        tail += [] if fresh else [text("orch_panel_stale", language)]
        if tail:
            # The age stays whole: at the end of the line when it fits, else under the value.
            suffix = " ".join(tail)
            last = "".join(value for value, style in found[-1])
            if cells(last) + cells(" · " + suffix) <= width:
                found[-1] = [*found[-1], (" · " + suffix, "")]
            else:
                found.append([(" " * min(cells(label + ": "), max(0, width - cells(suffix))), ""), (suffix, "")])
        for key, label in (("why", "orch_panel_why"), ("next", "orch_panel_next")):
            if isinstance(note.get(key), str) and note[key].strip():
                found += _labelled(text(label, language), note[key], width)
        if where := where_text(note.get("where"), language):
            found += _labelled("", where, width)
    count = panel.get("inbox")
    if count is None:
        count = "?" if waiting_human is None else waiting_human
    found.append(watch._line(text("orch_panel_for_you", language, count=count), "bold"))
    return found

"""The architect and docs rows and the role and lane introductions (lane-view m2).

The rows stand between the orchestrator's bar and the lane blocks, architect first. A
clicked role name or lane squirrel then explains itself in three rows, from the facts
collected in the snapshot. Only a caller-written short reason is shown; a role's full
why is never read here.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 11
REPLACES = ()
GAP = False
GAP_AFTER = False

ROLES = ("architect", "docs")
ROLE_GAP = "   "


def _age_parts(timestamp: str | None, now: datetime) -> tuple[str, int] | None:
    """(plural unit, floored count) on gates.age's boundaries; None without an aware time."""
    try:
        moment = datetime.fromisoformat(timestamp)  # type: ignore[arg-type]
        if moment.utcoffset() is None:
            return None
        seconds = max(0, int((now - moment).total_seconds()))
    except (TypeError, ValueError):
        return None
    for unit, size in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if seconds >= size:
            return unit, seconds // size
    return "seconds", seconds


def _facts(snapshot: Snapshot, role: str) -> tuple[bool, str | None, str | None]:
    """One role's (alive, short, last called); an absent or malformed entry was never called."""
    found = (snapshot.role_facts or {}).get(role)
    facts = found if isinstance(found, dict) else {}
    short, called = facts.get("short"), facts.get("last_called_at")
    return (bool(facts.get("alive")),
            short if isinstance(short, str) and short.strip() else None,
            called if isinstance(called, str) else None)


def _one_row(value: str) -> str:
    """Authored text as it was written, unless control characters would break the row."""
    from foragent import watch

    has_control = any(watch.unicodedata.category(char).startswith("C") for char in value)
    return watch._clean(value) if has_control else value


def _age(parts: tuple[str, int], language: str) -> str:
    from foragent import watch

    unit, count = parts
    return watch.text(f"lane_explainers_age_{unit}{'_one' if count == 1 else ''}", language, count=count)


def _segment(snapshot: Snapshot, role: str, language: str, now: datetime) -> tuple[str, str]:
    """A role's row text and its style; only an alive role is coloured."""
    from foragent import watch

    alive, short, called = _facts(snapshot, role)
    name = watch.text(f"lane_explainers_name_{role}", language)
    if alive:
        reason = _one_row(short) if short else watch.text("lane_explainers_no_short", language)
        return watch.text(f"lane_explainers_alive_{role}", language, short=reason), "working"
    parts = _age_parts(called, now)
    if parts is None:
        return watch.text("lane_explainers_never", language, name=name), ""
    return watch.text("lane_explainers_resting", language, name=name, age=_age(parts, language)), ""


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch, watch_scene
    from foragent.watch_sections.lane_bubble import _ellipsis

    width, top, now = max(1, context["width"]), context.get("top", 0), context["now"]
    hits, language = context.get("hits", []), config.language
    segments = [_segment(snapshot, role, language, now) for role in ROLES]
    together = sum(watch._cells(value) for value, _ in segments) + len(ROLE_GAP) <= width
    if together:
        rows = [[segments[0], (ROLE_GAP, ""), segments[1]]]
        places = [(0, top), (watch._cells(segments[0][0]) + len(ROLE_GAP), top)]
    else:
        mark = _ellipsis(config)
        rows = [watch_scene.fit_marked([segment], width, mark) for segment in segments]
        places = [(0, top), (0, top + 1)]
    for role, (x, y) in zip(ROLES, places):
        name = watch.text(f"lane_explainers_name_{role}", language)
        hits.append((x, y, min(width, x + watch._cells(name)), y + 1, ("role", role)))
    return rows


def _role_rows(role: str, snapshot: Snapshot, language: str, now: datetime) -> list[Line]:
    from foragent import watch

    alive, short, called = _facts(snapshot, role)
    parts = _age_parts(called, now)
    if alive:
        status = watch.text("lane_explainers_click_alive", language)
    elif parts is None:
        status = watch.text("lane_explainers_click_never", language)
    else:
        status = watch.text("lane_explainers_click_resting", language, age=_age(parts, language))
    return [[(watch.text(f"lane_explainers_about_{role}", language), "")], [(status, "")],
            [(short or watch.text("lane_explainers_no_short", language), "")]]


def _lane_rows(slug: str, snapshot: Snapshot, config: Config) -> list[Line]:
    from foragent import watch, watch_scene

    language = config.language
    facts = watch_scene.row_of(snapshot.lane_facts, slug)
    paths = watch_scene.owned_paths(snapshot, config, slug)
    finished, passed, reviewed = facts.get("week_finished"), facts.get("week_first_pass"), facts.get("week_reviewed")
    if finished is None:
        done = watch.text("lane_explainers_no_history", language)
    else:
        key = "lane_explainers_finished_one" if finished == 1 else "lane_explainers_finished"
        done = watch.text(key, language, count=finished)
    ratio = (watch.text("lane_explainers_first_pass", language, passed=passed, reviewed=reviewed)
             if reviewed and passed is not None else watch.text("lane_explainers_no_reviews", language))
    memory = facts.get("memory_latest")
    return [[(watch.text("lane_explainers_owns", language, paths=", ".join(paths)) if paths
              else watch.text("lane_explainers_no_paths", language), "")],
            [(f"{done} {ratio}", "")],
            [(memory if isinstance(memory, str) and memory.strip()
              else watch.text("lane_explainers_no_memory", language), "")]]


def explain(target: tuple[str, int | str | None], snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, width: int, state: dict | None) -> list[Line] | None:
    """Three rows for a role or a lane squirrel. Other targets are not this section's."""
    if not (isinstance(target, tuple) and len(target) == 2 and isinstance(target[1], str)):
        return None
    kind, name = target
    if kind == "role" and name in ROLES:
        return _role_rows(name, snapshot, config.language, now)
    if kind == "squirrel":
        return _lane_rows(name, snapshot, config)
    return None

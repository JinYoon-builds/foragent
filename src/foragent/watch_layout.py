"""Compose the lane view from the discovered foragent.watch_sections modules.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
The visible groups live in foragent/watch_sections/<name>.py; this module only
discovers them, stacks them by ORDER and keeps the helpers they share.
"""

from datetime import datetime
import importlib
from pathlib import Path
import pkgutil
from types import ModuleType
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot


# Only the pickaxe moves. One hit every four 0.3-second frames.
PICK = ("  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  ")


def _has_work(row: dict[str, Any]) -> bool:
    return (row["issue"] is not None or bool(row["queued"])
            or row["state"] in {"working", "blocked", "waiting-human"})


# Run phases in which the runner waits on checks, CI, a merge or a review.
# Waits inside them (merge.lock, a test slot) need no phase of their own.
WAITING_PHASES = frozenset({"checking", "ci", "merging", "reviewing", "overlap-wait", "test-slot-wait"})


def _activity(
    row: dict[str, Any], language: str, age: float, stall_minutes: float, pid: int | None,
    log_seconds: float | None = None, progress_seconds: float | None = None,
    phase_seconds: float | None = None,
) -> str:
    """The activity cell, judged by evidence: a live PID, the worker's last activity
    (its runner log when present) and the runner's own last progress (D67).

    A run looks stalled or quiet only when both the worker and the runner have been
    silent past the threshold. Otherwise a waiting phase shows the phase and its age.
    """
    from foragent import watch

    if row["state"] == "blocked":
        return "● " + watch.text("blocked", language)
    if row["state"] != "working":
        return ""
    if pid is not None:
        try:
            watch.os.kill(pid, 0)
        except ProcessLookupError:
            return watch.text("watch_runner_gone", language)
        except PermissionError:
            pass  # A process owned by another user still exists.
    limit = min(10, stall_minutes) * 60

    def silent(seconds: float | None) -> float | None:
        if seconds is None:
            return None
        quiet = seconds + age
        if progress_seconds is not None:
            quiet = min(quiet, progress_seconds + age)
        return quiet

    if log_seconds is not None:
        # The log's growth is the evidence; state metadata is not.
        quiet = silent(log_seconds)
        if quiet >= limit:
            return watch.text("watch_quiet", language, minutes=int(quiet // 60))
    else:
        quiet = silent(row["last_activity_seconds"])
        if quiet is not None and quiet > limit:
            return watch.text("watch_stalled", language, minutes=int(quiet // 60))
    if row.get("phase") in WAITING_PHASES and phase_seconds is not None:
        return watch.text("watch_in_phase", language, phase=watch.text(row["phase"], language),
                          minutes=int((phase_seconds + age) // 60))
    return "●"


def _widths(width: int) -> tuple[bool, int, int, int, int]:
    """Side-by-side flag, orchestrator, road, work-table and events widths."""
    side_by_side = width >= 100
    orchestrator_width = 28 if side_by_side else width
    road_width = width - orchestrator_width - 2 if side_by_side else width
    table_width = (width - 2) * 3 // 5 if side_by_side else width
    events_width = width - table_width - 2 if side_by_side else width
    return side_by_side, orchestrator_width, road_width, table_width, events_width


def _split(status: dict[str, Any]) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    """Lanes with and without work, numbered by their position among all lanes."""
    from foragent import watch

    rows = list(status["lanes"].values())
    busy = [(index, row) for index, row in enumerate(rows, 1) if watch._has_work(row)]
    idle = [(index, row) for index, row in enumerate(rows, 1) if not watch._has_work(row)]
    return busy, idle


def _clones(status: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Running runs by lane in start order; ties in start time break by issue number."""
    clones: dict[str, list[dict[str, Any]]] = {}
    running = [run for run in status["active_runs"] if run.get("status", "running") == "running"]
    for run in sorted(running, key=lambda run: (run.get("started_at") or "", run["number"])):
        clones.setdefault(run["lane"], []).append(run)
    return clones


def _lane_name(row: dict[str, Any], clones: dict[str, list[dict[str, Any]]]) -> str:
    """The plain lane name; workers are told apart by their own road rows."""
    return row["lane_display"]


def _worker_rows(row: dict[str, Any], clones: dict[str, list[dict[str, Any]]]) -> list[tuple[str, list[int]]]:
    """One road row per running worker, named '<lane> N' in start order when there are several."""
    workers = clones.get(row["slug"], [])
    if len(workers) < 2:
        return [(row["lane_display"], _lane_issues(row, clones))]
    numbers = [run["number"] for run in workers]
    # A blocked or waiting issue that is not a running worker keeps today's place on the first row.
    extra = [number for number in _lane_issues(row, clones) if number not in numbers]
    return [(f"{row['lane_display']} {position}", (extra if position == 1 else []) + [number])
            for position, number in enumerate(numbers, 1)]


def _lane_issues(row: dict[str, Any], clones: dict[str, list[dict[str, Any]]]) -> list[int]:
    """The lane's shown issues: its current issue first, or its workers in start order when it has several."""
    current = row["issue"] if row["state"] in {"working", "blocked", "waiting-human"} else None
    workers = [run["number"] for run in clones.get(row["slug"], [])]
    if len(workers) > 1:
        return workers + ([current] if current is not None and current not in workers else [])
    numbers = [current] if current is not None else []
    return numbers + [number for number in workers if number not in numbers]


def _sections() -> list[ModuleType]:
    """Discover every section module afresh, ordered by its unique ORDER."""
    package = importlib.import_module("foragent.watch_sections")
    found: dict[int, ModuleType] = {}
    for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if info.ispkg:
            continue
        module = importlib.import_module(info.name)
        order = module.ORDER
        if order in found:
            first, second = sorted((found[order].__name__, module.__name__))
            raise ValueError(f"watch sections {first} and {second} both use ORDER {order}")
        found[order] = module
    return [found[order] for order in sorted(found)]


def _frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int, width: int,
    selected: str | None, now: datetime, layout: dict[str, Any] | None = None,
) -> list[Line]:
    """Stack the sections by ORDER with one blank line between non-empty ones."""
    from foragent import watch

    hits: list[tuple[int, int, int, int, Any]] = []
    frame: list[Line] = []
    for section in _sections():
        gap = bool(frame) and getattr(section, "GAP", True)
        lines = section.render(
            snapshot, config, repo_root, tick=tick, width=width, selected=selected, now=now,
            layout=layout, top=len(frame) + gap, hits=hits,
        )
        if lines:
            frame += [[]] * gap + list(lines)
    if layout is not None:
        layout["hits"] = hits
    return [watch._fit(list(line), width) for line in frame]


def render_frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int = 0, width: int = 120,
    selected: str | None = None, now: datetime | None = None,
    state: dict[str, Any] | None = None, layout: dict[str, Any] | None = None,
) -> str:
    """A single plain-text frame, sharing the live view's layout and data."""
    from foragent import watch

    return "\n".join("".join(value for value, style in line).rstrip() for line in watch._frame(
        snapshot, config, repo_root, tick=tick, width=max(40, width), selected=selected,
        now=now or snapshot.captured_at, **({} if layout is None else {"layout": layout}),
    ))

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


# The lane-road pickaxe frames; the lane rows (#167) no longer animate, but foragent.watch keeps the name.
PICK = ("  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  ")


def _has_work(row: dict[str, Any]) -> bool:
    return (row["issue"] is not None or bool(row["queued"])
            or row["state"] in {"working", "blocked", "waiting-human"})


# Run phases in which the runner waits on checks, CI, a merge or a review.
# Waits inside them (merge.lock, a test slot) need no phase of their own.
WAITING_PHASES = frozenset({"checking", "ci", "merging", "reviewing", "overlap-wait",
                            "test-slot-wait", "baseline", "baseline-wait", "executor-wait",
                            "merge-lock-wait"})


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


def _clones(status: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Running runs by lane in start order; ties in start time break by issue number."""
    clones: dict[str, list[dict[str, Any]]] = {}
    running = [run for run in status["active_runs"] if run.get("status", "running") == "running"]
    for run in sorted(running, key=lambda run: (run.get("started_at") or "", run["number"])):
        clones.setdefault(run["lane"], []).append(run)
    return clones


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


# A terminal's usual height; a plain frame keeps to it.
SCREEN_ROWS = 24


def _frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int, width: int,
    selected: str | None, now: datetime, layout: dict[str, Any] | None = None, height: int | None = SCREEN_ROWS,
    state: dict[str, Any] | None = None, scrolls: bool = False,
) -> list[Line]:
    """Stack the sections by ORDER with one blank line between non-empty ones.

    Within ``height`` rows, sections marked KEEP (header, lane rows, details and
    footer) always stay; the others give way from the bottom up. A section
    marked STAYS (the fireworks band while it plays) stays too and is moved up into
    the first screen when the KEEP sections push it below. ``state`` is the live view's
    memory across frames (None for a stateless frame). ``scrolls`` (the live view)
    keeps every section, since the screen scrolls the whole frame; STAYS still moves.
    """
    from foragent import watch

    sections = _sections()
    rendered: list[tuple[ModuleType, list[Line], list[tuple[int, int, int, int, Any]]]] = []
    for section in sections:
        # Each section draws once from row 0; its click areas move with it below.
        own: list[tuple[int, int, int, int, Any]] = []
        lines = section.render(snapshot, config, repo_root, tick=tick, width=width, selected=selected,
                               now=now, layout=layout, top=0, hits=own, rows=True, height=height,
                               state=state)
        rendered.append((section, list(lines), own))
    # From its SIDE width on, a section (the orchestrator box) stands to the right
    # of the sections between the lane rows and itself.
    side = next((index for index, (section, _, _) in enumerate(rendered) if getattr(section, "SIDE", None)), None)
    wide = side is not None and width >= rendered[side][0].SIDE
    rows_end = max((index for index, (section, _, _) in enumerate(rendered)
                    if getattr(section, "KEEP", False) and (side is None or index < side)), default=-1)

    stays: list[tuple[int, int]] = []  # Row spans of the STAYS sections in the last assembly.

    def assemble() -> tuple[list[Line], list[tuple[int, int, int, int, Any]]]:
        hits: list[tuple[int, int, int, int, Any]] = []
        frame: list[Line] = []
        stays.clear()
        mark = None  # Where the sections beside the SIDE section begin.
        joined = False  # The previous section asked the next one to follow it directly.
        for index, (section, lines, own) in enumerate(rendered):
            if not lines:
                continue
            gap = bool(frame) and getattr(section, "GAP", True) and not joined
            joined = not getattr(section, "GAP_AFTER", True)
            if wide and index == side and mark is not None and mark < len(frame):
                left_width = width - max(watch._cells("".join(value for value, _ in line)) for line in lines) - 2
                left = frame[mark:]
                del frame[mark:]
                frame += watch._beside(left, lines, left_width)
                hits += [(x0 + left_width + 2, y0 + mark, x1 + left_width + 2, y1 + mark, target)
                         for x0, y0, x1, y1, target in own]
                continue
            top = len(frame) + gap
            if wide and mark is None and rows_end < index < side:
                mark = top
            frame += [[]] * gap + lines
            hits += [(x0, y0 + top, x1, y1 + top, target) for x0, y0, x1, y1, target in own]
            if getattr(section, "STAYS", False):
                stays.append((top, top + len(lines)))
        return frame, hits

    frame, hits = assemble()
    if height is not None and len(frame) > height and not scrolls:
        # A folded lane's last finished row and toggle give way first.
        for index, (section, lines, own) in enumerate(rendered):
            if getattr(section, "TIGHT", False):
                own = []
                rendered[index] = (section, list(section.render(
                    snapshot, config, repo_root, tick=tick, width=width, selected=selected, now=now,
                    layout=layout, top=0, hits=own, rows=True, height=height, state=state, tight=True)), own)
        frame, hits = assemble()
    if height is not None and len(frame) > height and not scrolls:
        # Lane rows win: the other sections give way from the bottom up.
        order = reversed(range(len(rendered)))
        for index in order:
            section, lines, _ = rendered[index]
            if lines and not (getattr(section, "KEEP", False) or getattr(section, "STAYS", False)):
                rendered[index] = (section, [], [])
                if len(assemble()[0]) <= height:
                    break
        # The footer becomes one short line of the key letters.
        for index, (section, lines, own) in enumerate(rendered):
            if section.__name__.endswith(".help"):
                own = []
                rendered[index] = (section, list(section.render(
                    snapshot, config, repo_root, tick=tick, width=width, selected=selected, now=now,
                    layout=layout, top=0, hits=own, rows=True, height=height, state=state, short=True)), own)
        frame, hits = assemble()
    if height is not None:
        # The live view shows the first height - 1 rows above the footer. A STAYS
        # section pushed below them by the KEEP sections moves up to just above the
        # footer, so it is on screen when the view opens; the rows it passes move down.
        for start, end in stays:
            size, target = end - start, height - 1 - (end - start)
            if end <= height - 1 or target < 0:
                continue
            frame[target:end] = frame[start:end] + frame[target:start]
            hits = [(x0, y0 + size, x1, y1 + size, item) if target <= y0 < start else (x0, y0, x1, y1, item)
                    for x0, y0, x1, y1, item in hits]
    if layout is not None:
        layout["hits"] = hits
    if state is not None:
        # Scrolling and the keyboard highlight read the frame's size and issue rows.
        state["length"] = len(frame)
        state["rows"] = sorted({(y0, target[1]) for x0, y0, x1, y1, target in hits
                                if isinstance(target, tuple) and target[0] == "acorn" and x1 > x0})
    return [watch._fit(list(line), width) for line in frame]


def _window(frame: list[Line], state: dict[str, Any] | None, height: int, width: int, language: str) -> list[Line]:
    """The rows a screen of ``height`` shows.

    A frame that fits is shown whole. A taller one shows ``height - 1`` rows from
    ``state['scroll']`` (kept within the last full screen) and, in place of the key
    help, a position line with the text-selection hint.
    """
    from foragent import watch

    state = {} if state is None else state
    state["height"] = height
    state["length"] = len(frame)
    if len(frame) <= height:
        state["scroll"] = 0
        return frame
    rows = max(1, height - 1)
    top = state["scroll"] = max(0, min(state.get("scroll", 0), watch.scroll_limit(state)))
    position = watch.text("acorns_position", language, first=top + 1, last=min(len(frame), top + rows),
                          total=len(frame))
    footer = watch._fit(watch._line(f"{position} · {watch.text('acorns_select', language)}", "dim"), width)
    return frame[top:top + rows] + [footer]


def render_frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int = 0, width: int = 120,
    selected: str | None = None, now: datetime | None = None,
    state: dict[str, Any] | None = None, layout: dict[str, Any] | None = None, height: int | None = None,
) -> str:
    """A single plain-text frame, sharing the live view's layout and data.

    With ``height``, only the rows a screen that tall shows: the frame scrolled to
    ``state['scroll']`` with a position line when it is taller than the screen.
    Hit boxes keep their whole-frame rows.
    """
    from foragent import watch

    frame = watch._frame(
        snapshot, config, repo_root, tick=tick, width=max(1, width), selected=selected,
        now=now or snapshot.captured_at, **({} if layout is None else {"layout": layout}),
        **({} if state is None else {"state": state}),
    )
    if height is not None:
        frame = watch._window(frame, state, max(1, height), max(1, width), config.language)
    return "\n".join("".join(value for value, style in line).rstrip() for line in frame)

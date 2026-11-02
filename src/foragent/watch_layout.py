"""Compose the lane view from the discovered foragent.watch_sections modules.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
The visible groups live in foragent/watch_sections/<name>.py; this module only
discovers them, stacks them by ORDER and keeps the helpers they share.
"""

from dataclasses import replace
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
    return (row.get("issue") is not None or bool(row.get("queued"))
            or row.get("state") in {"working", "blocked", "waiting-human"})


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
    # A record without its issue or lane belongs to no lane; the rest are still drawn.
    running = [run for run in status.get("active_runs") or () if isinstance(run, dict)
               and run.get("status", "running") == "running" and type(run.get("number")) is int and run.get("lane")]
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


# A terminal's usual height (kept for callers; frames are no longer cut to it).
SCREEN_ROWS = 24


def _frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int, width: int,
    selected: str | None, now: datetime, layout: dict[str, Any] | None = None, height: int | None = None,
    state: dict[str, Any] | None = None,
) -> list[Line]:
    """Stack the sections by ORDER with one blank line between non-empty ones.

    The frame is unbounded: every surviving row stays reachable, and the viewport
    (``_window``) applies a screen height once. Only with an explicit ``height`` that
    the frame overflows do TIGHT sections redraw compactly. ``state`` is the live
    view's memory across frames (None for a stateless frame).

    A section's ``REPLACES`` names other sections by their full module name: after every
    section has drawn, their rows, click areas, issue rows and layout roles are dropped.
    A section's ``PIN`` ('header' or 'footer') records its final rows in
    ``state['pinned_rows']``.
    """
    from foragent import watch

    from foragent.watch_sections import settings as settings_window

    discovered = _sections()
    # Declarations are unconditional: a section replaces its targets even when it draws
    # nothing or the settings window hides it. Absent names are ignored.
    replaced = {name for section in discovered for name in getattr(section, "REPLACES", ())}
    Hit = tuple[int, int, int, int, Any]
    Mark = tuple[int, int, str | None]  # (row, issue number, its lane; None when not foldable)
    rendered: list[tuple[ModuleType, list[Line], list[Hit], list[Mark]]] = []

    def draw(section: ModuleType, *, drawn: Config | None = None,
             **extra: Any) -> tuple[ModuleType, list[Line], list[Hit], list[Mark]]:
        # A section draws from row 0; its click areas and issue rows move with it below.
        own: list[Hit] = []
        marks: list[Mark] = []
        try:
            lines = section.render(snapshot, drawn or config, repo_root, tick=tick, width=width, selected=selected,
                                   now=now, layout=layout, top=0, hits=own, marks=marks, rows=True,
                                   height=height, state=state, **extra)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            # A replaced section's rows are dropped below, and it may need facts its
            # replacement does not: a snapshot without them must not stop the view.
            if section.__name__ not in replaced:
                raise
            return section, [], [], []
        return section, list(lines), own, marks

    for section in discovered:
        rendered.append(draw(section))
    # Every discovered section drew once in ORDER; replaced ones now leave no rows, hits,
    # marks or roles. Only then does the open settings window (#173) keep the header, its
    # window and the help line among the surviving contributions.
    rendered = [item for item in rendered if item[0].__name__ not in replaced]
    surviving = {id(item[0]): item for item in rendered}
    rendered = [surviving.get(id(section)) or draw(section)
                for section in settings_window.shown_sections([item[0] for item in rendered], state)]
    # From its SIDE width on, a section (the orchestrator box) stands to the right
    # of the sections between the lane rows and itself.
    side = next((index for index, (section, *_) in enumerate(rendered) if getattr(section, "SIDE", None)), None)
    wide = side is not None and width >= rendered[side][0].SIDE
    rows_end = max((index for index, (section, *_) in enumerate(rendered)
                    if getattr(section, "KEEP", False) and (side is None or index < side)), default=-1)

    pins: dict[str, list[int]] = {"header": [], "footer": []}  # Rows of PIN sections, likewise.

    def assemble() -> tuple[list[Line], list[Hit], list[Mark]]:
        hits: list[Hit] = []
        marks: list[Mark] = []
        frame: list[Line] = []
        for rows in pins.values():
            rows.clear()
        mark = None  # Where the sections beside the SIDE section begin.
        joined = False  # The previous section asked the next one to follow it directly.
        for index, (section, lines, own, rows) in enumerate(rendered):
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
                marks += [(y + mark, number, lane) for y, number, lane in rows]
                if getattr(section, "PIN", None) in pins:
                    pins[section.PIN] += range(mark, mark + len(lines))
                continue
            top = len(frame) + gap
            if wide and mark is None and rows_end < index < side:
                mark = top
            frame += [[]] * gap + lines
            if section is settings_window and state is not None and "settings_focus_rel" in state:
                first, last = state["settings_focus_rel"]
                state["settings_focus"] = (top + first, top + last)
            hits += [(x0, y0 + top, x1, y1 + top, target) for x0, y0, x1, y1, target in own]
            marks += [(y + top, number, lane) for y, number, lane in rows]
            if getattr(section, "PIN", None) in pins:
                pins[section.PIN] += range(top, top + len(lines))
        return frame, hits, marks

    if settings_window.is_open(state):
        # The open window: the header in its one-line form (no big squirrel), and the window
        # within the screen rows the header and the help line leave, so neither scrolls away.
        calm_config = replace(config, animation=False)
        rendered = [draw(section, drawn=calm_config) if section.__name__.endswith(".squirrels") else item
                    for item in rendered for section in item[:1]]
        screen = state.get("height")
        if screen is not None and screen >= watch.MIN_SCREEN:
            index = next(index for index, (section, *_) in enumerate(rendered) if section is settings_window)
            others = len(assemble()[0]) - len(rendered[index][1])
            rendered[index] = draw(settings_window, room=max(1, screen - others))
    frame, hits, marks = assemble()
    if height is not None and len(frame) > height:
        # An explicit height that overflows: a folded lane's last finished row and toggle
        # give way. Nothing else is dropped or moved; the viewport scrolls the rest.
        tight = False
        for index, (section, *_) in enumerate(rendered):
            if getattr(section, "TIGHT", False):
                rendered[index] = draw(section, tight=True)
                tight = True
        if tight:
            frame, hits, marks = assemble()
    if layout is not None:
        layout["hits"] = hits
        layout["offset"] = 0  # Hits are in F rows; a height render shifts them (``_window``).
        # The pinned rows travel with the layout too, so a height render without a
        # caller-owned state still keeps its header and footer on screen.
        layout["pinned_rows"] = {name: sorted(rows) for name, rows in pins.items()}
    if state is not None:
        # Interaction data comes once from the final rows, never from a section draw that a
        # later trim replaced: the frame's size, its issue rows for the highlight, and the
        # lane of each foldable row (completion rows have none, so f does nothing there).
        state["length"] = len(frame)
        state["rows"] = sorted({(y, number) for y, number, _ in marks})
        # Lanes belong to rows, not issue numbers: the same issue can show as a lane row
        # (foldable) and again as a completion row (not foldable).
        state["row_lanes"] = {y: lane for y, _, lane in marks if lane is not None}
        state["lanes"] = list(snapshot.status.get("lanes") or ())
        state["pinned_rows"] = {name: sorted(rows) for name, rows in pins.items()}
    return [watch._fit(list(line), width) for line in frame]


def _window(frame: list[Line], state: dict[str, Any] | None, height: int, width: int, language: str,
            layout: dict[str, Any] | None = None) -> list[Line]:
    """The rows a screen of ``height`` shows: one slice rule for render_frame and the live view.

    ``frame`` is F, the whole heightless frame. With pinned rows (``state['pinned_rows']``)
    ``watch_viewport.window`` keeps the header and footer on screen and scrolls the body.
    Without them, when F fits, it is returned as it is. When it is taller, the screen shows
    F[scroll : scroll + height - 1] and a position line with N = len(F); under
    MIN_SCREEN (3) rows it is F as it is, as with no height. ``scroll``
    stays within 0..len(F) - (height - 1), and ``layout['hits']`` moves to screen rows,
    clipped to the visible rows.
    """
    from foragent import watch

    from foragent import watch_viewport

    state = {} if state is None else state
    state.update(height=height, length=len(frame))
    pins = (layout or {}).get("pinned_rows") or state.get("pinned_rows") or {}
    if any(pins.values()):
        # Pinned header and footer rows: they stay put and only the body scrolls.
        return watch_viewport.window(frame, state, height, width, language, layout, pins)
    for name in ("body_length", "body_height", "row_map"):
        state.pop(name, None)
    if height < watch.MIN_SCREEN or len(frame) <= height:
        # F fits, or the screen is too short to scroll (under 3 rows counts as no height).
        state["scroll"] = 0
        return frame
    rows = height - 1
    top = state.get("scroll", 0)
    focus = state.get("settings_focus") if state.get("settings") else None
    if focus is not None:
        # The open settings window ignores scroll keys: follow its selected row and the
        # message, prompt and keys below it, keeping the row itself when all do not fit.
        first, last = focus
        top = min(first, max(top, last - rows))
    top = state["scroll"] = max(0, min(top, watch.scroll_limit(state)))
    if layout is not None:
        layout["offset"] = top  # Screen row + offset = F row.
        layout["hits"] = [(x0, max(0, y0 - top), x1, min(rows, y1 - top), target)
                          for x0, y0, x1, y1, target in layout.get("hits", [])
                          if max(0, y0 - top) < min(rows, y1 - top)]
    line = watch_viewport.position_line(top + 1, min(len(frame), top + rows), len(frame), width, language)
    return frame[top:top + rows] + [line]


def render_frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int = 0, width: int = 120,
    selected: str | None = None, now: datetime | None = None,
    state: dict[str, Any] | None = None, layout: dict[str, Any] | None = None, height: int | None = None,
) -> str:
    """A single plain-text frame, sharing the live view's layout and data.

    With ``height``, only the rows a screen that tall shows (``_window``): F, the
    whole heightless frame, sliced at ``state['scroll']`` with a position
    line when F is taller than the screen; hit boxes then use screen rows.
    """
    from foragent import watch

    if height is not None and state is not None:
        state["height"] = height  # The settings window fits itself to this screen.
    if height is not None and layout is None:
        # A private layout carries the frame's pinned rows to _window; sections draw the
        # same with or without one, and a stateless frame stays stateless.
        layout = {}
    frame = watch._frame(
        snapshot, config, repo_root, tick=tick, width=max(1, width), selected=selected,
        now=now or snapshot.captured_at, **({} if layout is None else {"layout": layout}),
        **({} if state is None else {"state": state}),
    )
    if height is not None:
        frame = watch._window(frame, state, height, max(1, width), config.language, layout)
    return "\n".join("".join(value for value, style in line).rstrip() for line in frame)

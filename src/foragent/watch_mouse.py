"""Mouse targets, keys and scrolling in the lane view.

render_frame fills a caller-owned layout with hit boxes; coordinates are
zero-based screen cells. Handlers change a caller-owned state: ``issue`` is the
open box's number (or None), ``scroll`` the zero-based first visible frame row,
``unfolded`` the lanes showing all their rows. Each frame records ``length``,
``rows`` (issue rows as (y, number)) and, on a screen, ``height``. Runtime
dependencies resolve through foragent.watch to preserve its patch points.
"""

from typing import Any

from foragent import orch_inbox
from foragent.config import Config
from foragent.state import StateStore


def hit_test(layout: dict[str, Any], x: int, y: int) -> Any:
    """The target under one screen cell, or None. Later boxes are drawn on top."""
    for left, top, right, bottom, target in reversed(layout.get("hits", [])):
        if left <= x < right and top <= y < bottom:
            return target
    return None


def scroll_limit(state: dict[str, Any]) -> int:
    """The last scroll offset: len(F) - (height - 1), else (before any screen) the last row."""
    length, height = state.get("length", 0), state.get("height")
    if height:
        return max(0, length - max(1, height - 1)) if length > height else 0
    return max(0, length - 1)


def scroll(state: dict[str, Any], rows: int) -> None:
    state["scroll"] = max(0, min(state.get("scroll", 0) + rows, scroll_limit(state)))


def highlighted_row(state: dict[str, Any]) -> int | None:
    """The frame row of the highlight: the top visible issue row, else the first issue row
    below it on screen. One row, chosen by position: the same issue shown again further
    down (or a wrapped second line) is not highlighted too.
    """
    issues = dict(state.get("rows", ()))
    top, height = state.get("scroll", 0), state.get("height")
    bottom = top + max(1, height - 1) if height and state.get("length", 0) > height else None
    return next((y for y in sorted(issues) if y >= top and (bottom is None or y < bottom)), None)


def highlighted(state: dict[str, Any]) -> int | None:
    """The issue number on the highlighted row."""
    from foragent import watch

    y = watch.highlighted_row(state)
    return None if y is None else dict(state.get("rows", ()))[y]


def _open(state: dict[str, Any], number: int | None, store: StateStore | None) -> None:
    """Open, switch or (with None) close the issue box; the box reads the records through store.

    The box starts at its top; closing it brings the rows back where they were scrolled.
    """
    was_open = state.get("issue") is not None
    if number is not None:
        if not was_open:
            state["rows_scroll"] = state.get("scroll", 0)
            state["scroll"] = 0
        state["issue"], state["store"] = number, store
    elif was_open:
        state["issue"] = None
        state["scroll"] = state.pop("rows_scroll", 0)


def handle_mouse(
    button: str, x: int, y: int, layout: dict[str, Any], store: StateStore, config: Config,
    *, state: dict[str, Any] | None = None,
) -> None:
    """Act on one mouse event. Wheel steps only scroll; a left click acts on its target."""
    from foragent import watch

    state = {} if state is None else state
    if button in ("wheel-down", "wheel-up"):
        watch.scroll(state, 1 if button == "wheel-down" else -1)
        return
    if button != "left":
        return
    target = watch.hit_test(layout, x, y)
    if target == ("orchestrator", None):
        watch.switch_to_orchestrator(store)
    elif isinstance(target, tuple) and target[0] == "acorn":
        # The same issue closes its box; another issue switches the box to it.
        _open(state, None if state.get("issue") == target[1] else target[1], store)
    elif isinstance(target, tuple) and target[0] == "more":
        toggle_lane(state, target[1])


def toggle_lane(state: dict[str, Any], slug: str) -> None:
    unfolded = set(state.get("unfolded", ()))
    unfolded ^= {slug}
    state["unfolded"] = unfolded


def handle_key(key: str, store: StateStore, config: Config, *, state: dict[str, Any] | None = None) -> bool:
    """Act on one key name; True asks the view to quit.

    j/k (and the arrows) move one row, Enter opens the highlighted row's box, f folds
    its lane, a unfolds every lane when any is folded (else folds all), o switches to
    the orchestrator, x or Esc closes the box (Esc quits when none is open). While a
    box is open, Enter, f and a do nothing.
    """
    from foragent import watch

    state = {} if state is None else state
    is_open = state.get("issue") is not None
    if key in ("q", "Q"):
        return True
    if key == "esc":
        if not is_open:
            return True
        _open(state, None, store)
    elif key == "x":
        _open(state, None, store)
    elif key in ("j", "down"):
        watch.scroll(state, 1)
    elif key in ("k", "up"):
        watch.scroll(state, -1)
    elif key in ("pagedown", "pageup"):
        step = max(1, (state.get("height") or 3) - 2)
        watch.scroll(state, step if key == "pagedown" else -step)
    elif key == "o":
        watch.switch_to_orchestrator(store)
    elif is_open:
        pass
    elif key == "a":
        lanes = set(config.lanes) | set(state.get("lanes", ()))
        unfolded = set(state.get("unfolded", ()))
        state["unfolded"] = set() if lanes <= unfolded else lanes | unfolded
    elif key in ("enter", "f"):
        number = watch.highlighted(state)
        if number is None:
            return False
        if key == "enter":
            _open(state, number, store)
        elif (slug := state.get("issue_lanes", {}).get(number)) is not None:
            watch.toggle_lane(state, slug)
    return False


def osc8(url: str, text: str) -> str:
    """Text that a terminal shows as a link to url (OSC 8)."""
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def terminal_setup_sequences() -> list[str]:
    """Written before curses starts: the alternate screen, so the view leaves no scrollback."""
    return ["\x1b[?1049h"]


def terminal_restore_sequences() -> list[str]:
    """Written when the view ends or re-executes: back to the main screen."""
    return ["\x1b[?1049l"]


def switch_to_orchestrator(store: StateStore) -> bool:
    """Switch to the Orca terminal named in the orchestrator's latest note.

    The destination is the recorded handle, never the pane running the view.
    Without a recorded Orca terminal, or without Orca, nothing happens.
    """
    from foragent import watch

    note = orch_inbox.read_note(store) or {}
    where = note.get("where") if isinstance(note.get("where"), dict) else {}
    handle = where.get("handle") if where.get("kind") == "orca" else None
    executable = watch.shutil.which("orca") if handle else None
    if not executable:
        return False
    try:
        watch.subprocess.run(
            [executable, "terminal", "switch", "--terminal", str(handle)],
            stdin=watch.subprocess.DEVNULL, stdout=watch.subprocess.DEVNULL,
            stderr=watch.subprocess.DEVNULL, check=True, timeout=5,
        )
    except (OSError, watch.subprocess.SubprocessError):
        return False  # A closed tab must not break the view.
    return True

"""Mouse targets, keys and scrolling in the lane view.

render_frame fills a caller-owned layout with hit boxes; coordinates are
zero-based screen cells. Handlers change a caller-owned state: ``explanation_target``
is the selected ('acorn', number), ('role', name) or ('squirrel', slug), ``scroll``
the zero-based first visible frame row, ``unfolded`` the lanes showing all their rows.
Legacy callers may supply ``issue`` and ``rows_scroll`` for closing an existing issue
box; inputs only select explanations. Each frame records ``length``,
``rows`` (issue rows as (y, number)) and, on a screen, ``height``. Runtime
dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
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


# A screen shorter than this is treated as no height: F as it is, no position line, no scrolling.
MIN_SCREEN = 3


def scroll_limit(state: dict[str, Any]) -> int:
    """The last scroll offset: len(F) - (height - 1); 0 under MIN_SCREEN rows; before any
    screen was drawn, the last row of the last frame. With pinned rows the scroll counts
    body rows only: body_length - body_height."""
    if "body_length" in state and "body_height" in state:
        return max(0, state["body_length"] - state["body_height"])
    length, height = state.get("length", 0), state.get("height")
    if height is None:
        return max(0, length - 1)
    if height < MIN_SCREEN or length <= height:
        return 0
    return length - (height - 1)


def scroll(state: dict[str, Any], rows: int) -> None:
    state["scroll"] = max(0, min(state.get("scroll", 0) + rows, scroll_limit(state)))


def _acorns(state: dict[str, Any]) -> list[int]:
    """The issue numbers of the frame's issue rows, each once, in the configured lane order
    (top to bottom within a lane; rows of no lane last). The scene may draw a lane group
    first (questions), but Up/Down keep the same steady order while lanes change groups."""
    lanes = {slug: index for index, slug in enumerate(state.get("lanes", ()))}
    row_lanes = state.get("row_lanes", {})
    rows = sorted(state.get("rows", ()),
                  key=lambda row: (lanes.get(row_lanes.get(row[0]), len(lanes)), row[0]))
    return list(dict.fromkeys(number for _, number in rows))


def selected(state: dict[str, Any]) -> int | None:
    """The chosen acorn: the one Up/Down (or a click) chose while it is still drawn, else
    the first one. Scrolling never changes it."""
    acorns = _acorns(state)
    if state.get("selected") in acorns:
        return state["selected"]
    return acorns[0] if acorns else None


def highlighted_row(state: dict[str, Any]) -> int | None:
    """The frame row of the chosen acorn: its first issue row, wherever the view is scrolled.
    One row: the same issue shown again further down (or a wrapped second line) is not
    highlighted too."""
    from foragent import watch

    number = watch.selected(state)
    return next((y for y, found in sorted(state.get("rows", ())) if found == number), None)


def highlighted(state: dict[str, Any]) -> int | None:
    """The issue number on the highlighted row."""
    from foragent import watch

    y = watch.highlighted_row(state)
    return None if y is None else dict(state.get("rows", ()))[y]


def _reveal(state: dict[str, Any], row: int) -> None:
    """Scroll as little as needed so frame row ``row`` is on screen."""
    if "body_length" in state and "body_height" in state:
        pinned = {pin for rows in (state.get("pinned_rows") or {}).values() for pin in rows}
        if row in pinned:
            return
        index, rows = row - sum(1 for pin in pinned if pin < row), state["body_height"]
    else:
        height = state.get("height")
        if height is None or height < MIN_SCREEN or state.get("length", 0) <= height:
            return
        index, rows = row, height - 1
    top = state.get("scroll", 0)
    if rows > 0 and not top <= index < top + rows:
        state["scroll"] = index if index < top else index - rows + 1
        scroll(state, 0)


def choose(state: dict[str, Any], step: int) -> None:
    """Choose the acorn ``step`` places after the chosen one (clamped) and bring it into view."""
    from foragent import watch

    acorns = _acorns(state)
    if not acorns:
        return
    current = watch.selected(state)
    state["selected"] = acorns[max(0, min(acorns.index(current) + step, len(acorns) - 1))]
    row = watch.highlighted_row(state)
    if row is not None:
        _reveal(state, row)


def handle_mouse(
    button: str, x: int, y: int, layout: dict[str, Any], store: StateStore, config: Config,
    *, state: dict[str, Any] | None = None,
) -> None:
    """Act on one mouse event. Wheel steps only scroll; a left click acts on its target."""
    from foragent import watch

    state = {} if state is None else state
    if button in ("wheel-down", "wheel-up"):
        # A wheel only moves the view: the chosen acorn and its explanation stay.
        state.pop("explain_scroll", None)
        watch.scroll(state, 1 if button == "wheel-down" else -1)
        return
    if button != "left":
        return
    target = watch.hit_test(layout, x, y)
    if isinstance(target, tuple) and target[0] in EXPLAINED:
        # The same target closes its explanation; another one switches to it.
        state.pop("explain_scroll", None)
        if target[0] == "acorn":
            state["selected"] = target[1]
        _select(state, target)
        return
    state.pop("explanation_target", None)  # Any other click clears the selection, then acts.
    state.pop("explanation_detail", None)
    if target == ("settings", None):
        # The header's text button opens the same window as the s key.
        if state.get("issue") is None:
            from foragent.watch_sections import settings as settings_window

            settings_window.open_window(state)
    elif target == ("orchestrator", None):
        watch.switch_to_orchestrator(store)
    elif isinstance(target, tuple) and target[0] == "more":
        toggle_lane(state, target[1])


# Click targets that select an explanation instead of acting. An acorn explains its issue
# in the orchestrator's bubble while every lane stays visible at its current scroll; the
# bubble's planning-request count lists the open requests there.
EXPLAINED = frozenset({"acorn", "role", "squirrel", "requests"})


def _select(state: dict[str, Any], target: tuple[str, Any]) -> None:
    """Select ``target``; the selected one again clears the selection."""
    state.pop("explanation_detail", None)
    if state.pop("explanation_target", None) != target:
        state["explanation_target"] = target


def _restore(state: dict[str, Any]) -> None:
    """Back to where the view was before Enter moved it to the explanation."""
    if "explain_scroll" in state:
        state["scroll"] = state.pop("explain_scroll")
        scroll(state, 0)


def toggle_lane(state: dict[str, Any], slug: str) -> None:
    unfolded = set(state.get("unfolded", ()))
    unfolded ^= {slug}
    state["unfolded"] = unfolded


def handle_key(key: str, store: StateStore | None, config: Config, *, state: dict[str, Any] | None = None,
               repo_root: Path | None = None, confirm: Callable[[str], bool] | None = None) -> bool:
    """Act on one key name; True asks the view to quit.

    Down/Up choose the next/previous acorn, j/k and PageDown/PageUp scroll, Enter toggles the chosen
    acorn's explanation (the view goes to it; closing comes back),
    f folds its lane, a toggles all folds, o switches to the orchestrator and m flips
    the Claude quota mode. x clears the explanation; Esc clears it or quits.
    s opens settings, which takes every key but q and m. ``confirm(message)`` approves
    a team change; otherwise settings asks y/n. ``repo_root`` is where it saves.

    For a caller-supplied legacy issue box, x/Esc restore its saved scroll and the
    modal Enter/f/a/m/s guards still apply. The live lane scene never opens a box.
    """
    from foragent import watch
    from foragent.watch_sections import settings as settings_window

    state = {} if state is None else state
    legacy_box_open = state.get("issue") is not None
    if key in ("q", "Q"):
        return True
    if settings_window.is_open(state) and not (key == "m" and state.get("settings_prompt") is None):
        settings_window.handle(key, store, config, state, repo_root, confirm)
        return False
    if key in ("esc", "x") and state.get("explanation_target") is not None:
        del state["explanation_target"]
        state.pop("explanation_detail", None)
        _restore(state)
    elif key in ("esc", "x"):
        if legacy_box_open:
            state["issue"] = None
            state["scroll"] = state.pop("rows_scroll", 0)
        elif key == "esc":
            return True
    elif key == "down":
        choose(state, 1)
    elif key == "up":
        choose(state, -1)
    elif key in ("j", "k"):
        # j/k scroll like the wheel; neither changes the chosen acorn.
        state.pop("explain_scroll", None)
        watch.scroll(state, 1 if key == "j" else -1)
    elif key in ("pagedown", "pageup"):
        step = max(1, (state.get("height") or 3) - 2)
        watch.scroll(state, step if key == "pagedown" else -step)
    elif key == "o":
        watch.switch_to_orchestrator(store)
    elif legacy_box_open:
        pass
    elif key == "s":
        state.pop("explanation_target", None)
        state.pop("explanation_detail", None)
        settings_window.open_window(state)
    elif key == "m":
        # The same flip as `fa quota mode`: from the effective mode to the other one.
        from foragent import scheduler_quota

        if store is None:
            return False  # No repository state to hold the override.

        mode = "burn" if scheduler_quota.quota_mode(store, config)[0] == "keep" else "keep"
        store.set_claude_quota_mode(mode)
        state["quota_mode"] = (mode, datetime.now(UTC))  # The usage line shows it before the next refresh.
    elif key == "a":
        lanes = set(config.lanes) | set(state.get("lanes", ()))
        unfolded = set(state.get("unfolded", ()))
        state["unfolded"] = set() if lanes <= unfolded else lanes | unfolded
    elif key in ("enter", "f"):
        # The highlighted row itself, not its issue number, decides: a completion row of an
        # issue that also shows as a lane row is still not foldable.
        row = watch.highlighted_row(state)
        if row is None:
            return False
        if key == "enter":
            # The chosen acorn is explained, as a click on it would be. The explanation is at
            # the top of the view, so the view goes there and x (or Enter again) comes back.
            target = ("acorn", dict(state.get("rows", ()))[row])
            state.pop("explanation_detail", None)  # The selection changes: its loaded facts go.
            if state.get("explanation_target") == target:
                del state["explanation_target"]
                _restore(state)
            else:
                state["explanation_target"] = target
                state.setdefault("explain_scroll", state.get("scroll", 0))
                state["scroll"] = 0
        elif (slug := state.get("row_lanes", {}).get(row)) is not None:
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

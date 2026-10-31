"""The lane view's viewport: pinned header and footer rows around a scrolling body.

``watch_layout._frame`` builds F, the whole frame. ``window`` cuts it to a screen:
header pins at the top, then the body slice at ``state['scroll']``, then (only while the
body overflows) one position row, then the footer pins. ``layout['row_map']`` maps each
screen row to its F row; hits move through the same map.
"""

from typing import Any

from foragent.watch_cells import Line


def position_line(first: int, last: int, total: int, width: int, language: str) -> Line:
    """'rows first–last of total · Option-drag to select text', dim and cut to the width."""
    from foragent import watch

    value = (watch.text("acorns_position", language, first=first, last=last, total=total)
             + " · " + watch.text("acorns_select", language))
    return watch._fit(watch._line(value, "dim"), max(1, width))


def _remap(hits: list, row_map: dict[int, int]) -> list:
    """Hits in F rows, as one-row hits on every screen row that shows their F rows."""
    screen = {row: y for y, row in row_map.items()}
    moved = []
    for x0, y0, x1, y1, target in hits:
        for row in range(y0, y1):
            if row in screen:
                moved.append((x0, screen[row], x1, screen[row] + 1, target))
    return moved


def _finish(state: dict[str, Any], layout: dict[str, Any] | None, row_map: dict[int, int],
            pinned: bool = True) -> None:
    if pinned:
        state["row_map"] = row_map
    else:
        state.pop("row_map", None)  # The frame from its top: F rows are screen rows.
    if layout is not None:
        layout["row_map"] = row_map
        layout["hits"] = _remap(layout.get("hits", []), row_map)


def window(frame: list[Line], state: dict[str, Any], height: int, width: int, language: str,
           layout: dict[str, Any] | None, pins: dict[str, list[int]]) -> list[Line]:
    """The screen rows of a frame with pinned rows (see the module text)."""
    header = [row for row in pins.get("header", []) if 0 <= row < len(frame)]
    footer = [row for row in pins.get("footer", []) if 0 <= row < len(frame) and row not in header]
    pinned = set(header) | set(footer)
    body = [row for row in range(len(frame)) if row not in pinned]
    state["body_length"] = len(body)
    if height < 4 or len(frame) <= height:
        # It fits, or the screen is too short for pins: the frame from its top, no scrolling.
        shown = list(range(min(height, len(frame)) if height < 4 else len(frame)))
        state["scroll"] = 0
        state["body_height"] = len(body)
        _finish(state, layout, {y: row for y, row in enumerate(shown)}, pinned=height >= 4)
        return [frame[row] for row in shown]
    position = height >= 5
    rows = max(0, height - len(header) - len(footer) - position)
    state["body_height"] = rows
    top = state.get("scroll", 0)
    focus = state.get("settings_focus") if state.get("settings") else None
    if focus is not None:
        # The open settings window ignores scroll keys: follow its selected row and the
        # rows below it, keeping the row itself when all do not fit.
        first, last = (sum(1 for row in body if row < edge) for edge in focus)
        top = min(first, max(top, last - rows))
    top = state["scroll"] = max(0, min(top, len(body) - rows))
    visible = body[top:top + rows]
    shown: list[Line] = [frame[row] for row in header] + [frame[row] for row in visible]
    row_map = {y: row for y, row in enumerate([*header, *visible])}
    if position:
        shown.append(position_line(top + 1, top + len(visible), len(body), width, language))
    for row in footer:
        row_map[len(shown)] = row
        shown.append(frame[row])
    _finish(state, layout, row_map)
    return shown

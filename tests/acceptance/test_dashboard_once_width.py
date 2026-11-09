"""Acceptance test for n75-dashboard-once-width: a one-shot dashboard fits its pane.

`fa dashboard --once` used to print the renderer's 120-column default frame whatever the
pane width (src/foragent/watch.py:105, src/foragent/watch_layout.py:316). After the issue:

- At terminal widths of 40 and 80 columns, `fa dashboard --once` exits with code 0 and
  each printed frame line occupies at most that many terminal cells, including dividers
  and Korean text.
- For the same populated snapshot in a pane too narrow for the squirrel and bubble side
  by side, the one-shot output places the squirrel above the bubble and keeps the issue's
  visible state text.

The pane width is given the way a terminal gives it to a non-interactive process: the
COLUMNS environment variable (what `shutil.get_terminal_size` reads first; captured
output is not a TTY). Cell widths come from an oracle independent of Foragent's own
counter (tests/helpers/cells.py plus emoji presentation for VS16 and the squirrel).
The interactive narrow layout regression control lives in
test_dashboard_once_width_guard.py (it already passes on main).
"""

import re

import pytest

from dashboard_once_helpers import WIDTHS, PIXELS, _once, _populated, terminal_cells
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)
from test_squirrel_running import utf8_terminal, view_offline  # noqa: F401  (fixtures)


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("width", WIDTHS)
def test_one_shot_dashboard_rows_fit_the_pane(git_history, tmp_path, monkeypatch, capsys, utf8_terminal,
                                              view_offline, language, width):
    case, main = _populated(git_history, tmp_path, monkeypatch, language)
    code, rows = _once(main, case, capsys, width)
    assert code in (0, None), f"fa dashboard --once exits 0 at {width} columns, got {code}"
    assert rows, f"fa dashboard --once printed a frame at {width} columns"
    text = "\n".join(rows)
    assert "#2" in text, f"the populated frame shows the running issue:\n{text}"
    if language == "ko":
        assert re.search(r"[가-힣]", text), f"the Korean frame shows Korean text:\n{text}"
    over = [(terminal_cells(row), row) for row in rows if terminal_cells(row) > width]
    assert not over, (
        f"at {width} columns these printed rows (dividers and Korean included) are wider than the "
        f"pane, so the terminal wraps them: {over}")


def _hero(rows):
    """(squirrel rows, bubble top row, bubble bottom row) of the hero area.

    The bubble is the first box (╭ … ╰); the big squirrel is every row with pixel glyphs
    before the bubble's bottom border (the lane blocks below carry their own small art).
    """
    tops = [index for index, row in enumerate(rows) if "╭─" in row]
    bottoms = [index for index, row in enumerate(rows) if "╰─" in row]
    if not tops or not bottoms:
        return [], None, None
    top = tops[0]
    bottom = next((index for index in bottoms if index > top), None)
    if bottom is None:
        return [], top, None
    sprite = [index for index in range(bottom + 1) if PIXELS & set(rows[index])]
    return sprite, top, bottom


def _state_text(rows):
    """The running issue #2 as the frame states it: its number and step word."""
    for row in rows:
        match = re.search(r"#2 ([^\s▌|\]]+)", row)
        if match:
            return "#2 " + match.group(1)
    return None


@pytest.mark.parametrize("language", ["ko", "en"])
def test_narrow_one_shot_puts_the_squirrel_above_the_bubble(git_history, tmp_path, monkeypatch, capsys,
                                                            utf8_terminal, view_offline, language):
    case, main = _populated(git_history, tmp_path, monkeypatch, language)
    # Control: a wide pane keeps the squirrel beside its bubble (same snapshot, same command).
    code, wide = _once(main, case, capsys, 160)
    assert code in (0, None)
    text = "\n".join(wide)
    sprite, top, bottom = _hero(wide)
    assert sprite and top is not None and bottom is not None, text
    assert top in sprite, f"at 160 columns the squirrel stands beside the bubble's top border:\n{text}"
    state = _state_text(wide)
    assert state, f"the populated frame states the running issue's step:\n{text}"

    for width in (40, 60):
        code, rows = _once(main, case, capsys, width)
        assert code in (0, None), f"fa dashboard --once exits 0 at {width} columns"
        text = "\n".join(rows)
        sprite, top, bottom = _hero(rows)
        assert top is not None and bottom is not None, f"at {width} columns the bubble is drawn:\n{text}"
        assert sprite, f"at {width} columns the squirrel is still drawn:\n{text}"
        beside = [index for index in sprite if index >= top]
        assert not beside, (
            f"at {width} columns the squirrel stands above the bubble, never beside it "
            f"(rows {beside} share a row with the bubble):\n{text}")
        assert max(sprite) < top, text
        assert all(terminal_cells(row) <= width for row in rows), (
            f"at {width} columns every row fits the pane:\n{text}")
        assert state in text, f"at {width} columns the issue's visible state text {state!r} stays:\n{text}"

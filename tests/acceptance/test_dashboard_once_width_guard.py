"""Regression control for n75-dashboard-once-width (passes on main; not a Done when file).

The live curses dashboard at 40 and 80 columns keeps its existing narrow layout: every
painted span stays inside the screen and the running issue is drawn.
"""

import sys
from io import StringIO

import pytest

from dashboard_once_helpers import WIDTHS, _populated, terminal_cells
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)
from test_squirrel_running import utf8_terminal, view_offline  # noqa: F401  (fixtures)


class _TTY(StringIO):
    def isatty(self):
        return True


class _Screen:
    def __init__(self, height, width):
        self.size = height, width
        self.frames, self.current = [], []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, attribute=0):
        self.current.append((y, x, value))

    def refresh(self):
        if self.current:
            self.frames.append(self.current)
        self.current = []

    def getch(self):
        return ord("q") if self.frames else -1

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.mark.parametrize("language", ["ko", "en"])
def test_interactive_narrow_layout_still_fits_and_stacks(git_history, tmp_path, monkeypatch, capsys,
                                                         utf8_terminal, view_offline, language):
    """Regression control: the live view at 40 and 80 columns keeps its narrow layout."""
    import curses

    from foragent import watch

    case, main = _populated(git_history, tmp_path, monkeypatch, language)
    for width in WIDTHS:
        screen = _Screen(60, width)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(curses, "wrapper", lambda function, *a, **k: function(screen, *a, **k))
            patch.setattr(curses, "has_colors", lambda: False)
            patch.setattr(curses, "curs_set", lambda value: None)
            patch.setattr(watch, "_installed_build", lambda: "offline")
            patch.setattr(sys, "stdin", _TTY())
            patch.setattr(sys, "stdout", _TTY())
            assert main(["dashboard", "--repo-root", str(case.root)]) in (0, None)
        screen.refresh()
        assert screen.frames, width
        items = screen.frames[0]
        assert any("#2" in value for _y, _x, value in items), width
        outside = [(y, x, value) for y, x, value in items if value and x + terminal_cells(value) > width]
        assert not outside, f"the live view at {width} columns stays inside the screen: {outside[:5]}"

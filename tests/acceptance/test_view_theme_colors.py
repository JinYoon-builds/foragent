"""Acceptance test for view-theme-colors. Locked by Foragent; do not edit."""

import curses
import sys

import pytest

from test_skills_autosync import isolated_home


@pytest.mark.parametrize("colors", [8, 256])
def test_view_theme_colors_all_backgrounds_use_terminal_default(colors):
    from foragent.watch import color_pairs

    pairs = color_pairs(colors)
    assert isinstance(pairs, list) and pairs
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in pairs)
    assert all(background == -1 for foreground, background in pairs)


@pytest.mark.parametrize("colors", [8, 256])
def test_view_theme_colors_palette_has_no_yellow_or_orange_foregrounds(colors):
    from foragent.watch import color_pairs

    pairs = color_pairs(colors)
    assert pairs
    assert not {foreground for foreground, background in pairs} & {
        curses.COLOR_YELLOW, 3, 11, 178, 184, 208, 214, 220, 226}


@pytest.mark.parametrize("colors", [8, 256])
def test_view_theme_colors_curses_setup_uses_public_palette_and_default_colors(tmp_path, monkeypatch, colors):
    from foragent import watch
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    # A fake screen exercises the real watch entry point without touching a terminal.
    class Screen:
        def getmaxyx(self):
            return 50, 120

        def getch(self):
            return ord("q")

        def __getattr__(self, name):
            return lambda *args: None

    calls, defaults = [], []
    monkeypatch.setattr(curses, "COLORS", colors, raising=False)
    monkeypatch.setattr(curses, "wrapper", lambda fn, *args: fn(Screen(), *args))
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: defaults.append(True))
    monkeypatch.setattr(curses, "init_pair", lambda index, fg, bg: calls.append((fg, bg)))
    monkeypatch.setattr(curses, "color_pair", lambda index: 0)
    monkeypatch.setattr(curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    status = {"lanes": {}, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [], "history": []}
    watch.run(lambda: status, FakeTracker(), StateStore(tmp_path / "state"),
              Config(skill_libraries=[]), tmp_path)
    assert defaults == [True], "curses must enable the terminal's default background"
    assert calls == watch.color_pairs(colors)

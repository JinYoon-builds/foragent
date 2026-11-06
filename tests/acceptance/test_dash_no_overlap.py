"""Acceptance test for dash-no-overlap: dashboard text never overlaps at supported widths.

Jin 2026-11-05 (screenshot): labels in the lane view overlapped each other. At every
supported terminal width no two text items of the main screen occupy the same cells,
and a long title is shortened (with a mark) instead of running into its neighbours.

The checks do not pin a layout (#2078 owns it). They use three layout-free facts:
- every printed row fits the width a terminal really gives its characters;
- in the live view, every text item curses paints (one addstr call) stays inside the
  screen and shares no cell with another item of the same frame;
- a long lane title that does not fit is cut to a prefix that ends with an ellipsis,
  on its own row, and is never dropped or wrapped onto the next row.

Cell widths come from an oracle independent of Foragent's own counter: Unicode East
Asian Width (tests/helpers/cells.py), plus emoji presentation: a character followed
by VARIATION SELECTOR-16 (U+FE0F; e.g. U+1F6E0 U+FE0F, the hammer and wrench emoji)
is drawn two cells wide by terminals.
"""

from datetime import timedelta
from io import StringIO
import os
import sys

import pytest

from helpers.cells import cells as east_asian_cells
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)
from test_squirrel_running import utf8_terminal, view_offline  # noqa: F401  (fixtures)


# From the narrowest text mode through wide terminals; odd widths put a wide
# character on the last cell boundary (decisions.md: 56+ side by side, 44-55 stacked,
# under 44 text mode).
WIDTHS = (40, 44, 55, 56, 79, 80, 100, 120, 160, 200)
VS16 = "\ufe0f"

LONG_KO = "데이터 파이프라인과 대시보드 화면을 함께 맡는 아주 긴 레인 이름입니다"
LONG_EN = "Data pipeline and dashboard screens shared by one very long lane name indeed"


def terminal_cells(value: str) -> int:
    """Cells a terminal draws: East Asian Width, and two for an emoji presentation."""
    total = 0
    for index, char in enumerate(value):
        if char == VS16:
            continue
        width = east_asian_cells(char)
        if index + 1 < len(value) and value[index + 1] == VS16 and width == 1:
            width = 2
        total += width
    return total


def _busy_dashboard(git_history, tmp_path, monkeypatch, capsys, *, language):
    """A repository like Jin's: many lanes, long titles and paths, emoji with VS16,
    several running issues per lane, a question and queued work. Returns the case,
    the bound CLI and the snapshot `fa gate watch` itself rendered."""
    from foragent import watch
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    long_title = LONG_KO if language == "ko" else LONG_EN
    lanes = {
        "api": {"title": long_title, "emoji": "\U0001f6e0" + VS16,
                "paths": [f"src/foragent/very_long_module_name_{i}/**" for i in range(6)]},
        "web": {"title": "Web " + "w" * 90, "emoji": "\u2699" + VS16, "paths": ["web/**", "src/web_*.py"]},
        "docs": {"title": "문서" if language == "ko" else "Docs", "emoji": "🧩", "paths": ["docs/**"]},
        "board": {"paths": ["src/foragent/board*.py", "src/foragent/init.py", "src/foragent/order.py"]},
        "review-engine": {"paths": ["src/foragent/run_review.py", "src/foragent/review_*.py"]},
        "skills": {"paths": ["src/foragent/skills.py", "src/foragent/lanes.py"]},
    }
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    main = bind_cli(case, monkeypatch)
    phases = ("implementing", "reviewing", "checking", "ci", "merging")
    number = 1
    for index, slug in enumerate(("api", "web", "docs", "board")):
        for offset in range(3 if index < 2 else 1):
            number += 1
            title = ("대시보드 글자가 겹치지 않게 하는 아주 긴 이슈 제목 " if language == "ko"
                     else "Make the dashboard text never overlap at any width ") * 3
            case.tracker.add_issue(number, title, labels=["approved", f"lane:{slug}", "running"])
            case.store.start_run(number, branch=f"foragent/{number}", worktree=tmp_path / f"wt{number}",
                                 lane=slug)
            case.store.update_run(number, lane=slug, phase=phases[(index + offset) % len(phases)],
                                  pid=os.getpid(),
                                  phase_started_at=(case.clock[0] - timedelta(seconds=150)).isoformat(),
                                  last_activity_at=(case.clock[0] - timedelta(seconds=40)).isoformat())
    for slug in ("review-engine", "skills", "web"):
        for _ in range(4):
            number += 1
            case.tracker.add_issue(number, "Waiting work with a long title " * 6,
                                   labels=["approved", f"lane:{slug}"])
    number += 1
    case.tracker.add_issue(number, "질문이 있는 이슈 " * 6, labels=["approved", "needs-human", "lane:docs"])
    tick(tick_context(case, start_runs=False))

    seen, real = {}, watch.render_frame

    def capture(snapshot, config, root, **kwargs):
        seen.update(snapshot=snapshot, config=config)
        return real(snapshot, config, root, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(watch, "render_frame", capture)
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    printed = capsys.readouterr().out
    assert seen, "fa gate watch --once renders the main screen"
    return case, main, seen["snapshot"], seen["config"], printed


def _frame(case, snapshot, config, width):
    from foragent import watch

    return watch.render_frame(snapshot, config, case.root, width=width, now=case.clock[0])


@pytest.mark.parametrize("language", ["ko", "en"])
def test_main_screen_rows_fit_every_supported_width(git_history, tmp_path, monkeypatch, capsys, language):
    case, _main, snapshot, config, printed = _busy_dashboard(git_history, tmp_path, monkeypatch, capsys,
                                                             language=language)
    printed_rows = printed.rstrip("\n").splitlines()
    assert printed_rows, "the printed dashboard has rows"
    wide = [row for row in printed_rows if terminal_cells(row) > 120]
    assert not wide, f"`fa gate watch --once` (120 columns) rows run past the edge: {wide}"
    for width in WIDTHS:
        rows = _frame(case, snapshot, config, width).splitlines()
        assert rows, width
        over = [(terminal_cells(row), row) for row in rows if terminal_cells(row) > width]
        assert not over, (
            f"at {width} columns these rows are wider than the terminal, so the terminal wraps "
            f"them onto the next row's text: {over}")


class _TTY(StringIO):
    def isatty(self):
        return True


class _Screen:
    """A curses screen that records what each frame paints; q after the first frame."""

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


def _live_frame(case, main, monkeypatch, width, height=50):
    """The first frame `fa gate watch` paints on a curses screen `width` columns wide."""
    import curses

    from foragent import watch

    screen = _Screen(height, width)

    def wrapper(function, *args, **kwargs):
        return function(screen, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(curses, "wrapper", wrapper)
        patch.setattr(curses, "has_colors", lambda: False)
        patch.setattr(curses, "curs_set", lambda value: None)
        patch.setattr(watch, "_installed_build", lambda: "offline")
        patch.setattr(sys, "stdin", _TTY())
        patch.setattr(sys, "stdout", _TTY())
        assert main(["gate", "watch", "--repo-root", str(case.root)]) in (0, None)
    screen.refresh()
    assert screen.frames, f"the live view painted a frame at {width} columns"
    return screen.frames[0]


def _collisions(items, width):
    """(row, cell, first item, second item) for every cell two items paint, and items past the edge."""
    owner, shared, outside = {}, [], []
    for index, (y, x, value) in enumerate(items):
        if not value:
            continue
        end = x + terminal_cells(value)
        if x < 0 or end > width:
            outside.append((y, x, end, value))
        for cell in range(x, end):
            if (y, cell) in owner and owner[(y, cell)] != index:
                shared.append((y, cell, items[owner[(y, cell)]][2], value))
            owner[(y, cell)] = index
    return shared, outside


@pytest.mark.parametrize("language", ["ko", "en"])
def test_live_main_screen_items_never_share_cells(git_history, tmp_path, monkeypatch, capsys, language):
    case, main, _snapshot, _config, _printed = _busy_dashboard(git_history, tmp_path, monkeypatch, capsys,
                                                               language=language)
    for width in WIDTHS:
        items = _live_frame(case, main, monkeypatch, width)
        assert any(value.strip() for _y, _x, value in items), width
        shared, outside = _collisions(items, width)
        assert not outside, f"at {width} columns these text items run past the screen edge: {outside[:5]}"
        assert not shared, f"at {width} columns two text items occupy the same cells: {shared[:5]}"


@pytest.mark.parametrize("language", ["ko", "en"])
def test_long_lane_title_is_shortened_on_its_own_row(git_history, tmp_path, monkeypatch, capsys, language):
    case, _main, snapshot, config, _printed = _busy_dashboard(git_history, tmp_path, monkeypatch, capsys,
                                                              language=language)
    title = LONG_KO if language == "ko" else LONG_EN
    first_word, tail = title.split()[0], title.split()[-1]
    for width in WIDTHS:
        rows = _frame(case, snapshot, config, width).splitlines()
        named = [row for row in rows if first_word in row]
        assert named, f"at {width} columns the long lane title is shortened, not dropped"
        row = named[0]
        assert terminal_cells(row) <= width, (width, row)
        shown = row[row.index(first_word):].rstrip()
        if title in shown:
            continue
        cut = shown.split("…")[0] if "…" in shown else shown.split("...")[0]
        assert "…" in shown or "..." in shown, (
            f"at {width} columns a title that does not fit ends with an ellipsis: {row!r}")
        assert title.startswith(cut.rstrip()), (
            f"at {width} columns the shortened title is the start of the title: {row!r}")
        below = rows[rows.index(row) + 1:rows.index(row) + 3]
        assert not any(tail in other for other in below), (
            f"at {width} columns the title's end wrapped onto the next row: {below}")
    assert any(title in row for row in _frame(case, snapshot, config, 400).splitlines()), (
        "a terminal wide enough shows the whole title")

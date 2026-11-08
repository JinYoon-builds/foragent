"""Acceptance tests for n81-issue-view-width: fit focused issue dashboard lines to the pane.

`fa dashboard <N>` read the terminal width but printed its assembled rows without a final
width bound: a lane's path list, a long title, a held run's detail and a reviewer's verdict
ran past a 40- or 80-cell pane. After the issue every displayed line fits the pane in
terminal cells (wide characters two, ANSI and hyperlink decoration zero), in English and
Korean, for the live explanation, the finished-run history and a closed issue's completion
rows, while the issue's identity and its current state stay readable.

Real CLI, gate tick and local state; only GitHub, the clock and (for the live pane) curses
are fake. Helpers live in focused_issue_width.py.
"""

import re
import sys
import threading
import time

import pytest

from focused_issue_width import (CLOSED, CLOSED_TITLES, LANE_TITLES, MERGED, QUEUED, REQUEUED, RUNNING,
                                 TITLES, cells, run_cli, show, visible, world)

MERGED_WORDS = {
    "en": re.compile(r"\bmerged\b", re.I),
    "ko": re.compile(r"병합됐|병합되었|병합 완료|병합을 마쳤"),
}
CLOSED_WORDS = {
    "en": re.compile(r"\bclosed\b", re.I),
    "ko": re.compile(r"닫혔|닫았|닫힌"),
}


def _too_wide(out, width):
    return [(cells(line), visible(line)) for line in out.splitlines() if cells(line) > width]


def _identity_kept(out, number, title):
    """A line starts with the issue number followed by the start of its title."""
    head = visible(title)[:2]
    return any(visible(line).startswith(f"#{number} {head}") for line in out.splitlines())


@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("number", [RUNNING, QUEUED, REQUEUED],
                         ids=["running-lane-paths-pr", "waiting-for-dependency", "finished-held-run"])
def test_once_output_fits_the_terminal_cells(git_history, tmp_path, monkeypatch, capsys,
                                            width, language, number):
    """Long title, lane paths, wait reason, run detail, review text and PR: every line fits."""
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    code, wide, err = show(main, capsys, monkeypatch, number, 240)
    assert code == 0, f"Setup: `fa dashboard {number} --once` prints the issue: {err}"
    assert "REQUEST-CHANGES" in wide, f"Setup: the long reviewer verdict is shown:\n{wide}"
    if number == REQUEUED:
        assert "DETAIL-7" in wide, f"Setup: the finished-run branch shows the held run's detail:\n{wide}"
    else:
        assert "github.com" in wide, f"Setup: the live explanation shows a link:\n{wide}"

    code, out, err = show(main, capsys, monkeypatch, number, width)
    assert code == 0, f"`fa dashboard {number} --once` at {width} cells must exit 0: {err}"
    too_wide = _too_wide(out, width)
    assert not too_wide, (
        f"At {width} cells every line of `fa dashboard {number} --once` must fit the terminal "
        f"(cells, wide characters two, decoration zero); too wide: {too_wide}\n{out}")
    assert _identity_kept(out, number, TITLES[language]), (
        f"The issue's identity (#{number} and the start of its title) stays visible:\n{out}")


@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("number,state", [(MERGED, "merged"), (CLOSED, "closed")], ids=["merged", "closed"])
def test_closed_issue_completion_rows_fit_and_keep_identity_and_state(git_history, tmp_path, monkeypatch,
                                                                      capsys, width, language, number, state):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    row = next(row for row in case.store.local_issues() if row.get("number") == number)
    assert row.get("state") == "closed", f"Setup: the gate recorded #{number} closed: {row}"
    code, wide, err = show(main, capsys, monkeypatch, number, 240)
    assert code == 0, err
    words = MERGED_WORDS if state == "merged" else CLOSED_WORDS
    assert words[language].search(wide), f"Setup: the wide view states #{number} is {state}:\n{wide}"

    code, out, err = show(main, capsys, monkeypatch, number, width)
    assert code == 0, f"`fa dashboard {number} --once` at {width} cells must exit 0: {err}"
    too_wide = _too_wide(out, width)
    assert not too_wide, (
        f"At {width} cells the closed issue's completion rows must fit (Korean and wide "
        f"Unicode count two cells); too wide: {too_wide}\n{out}")
    assert _identity_kept(out, number, CLOSED_TITLES[language]), (
        f"#{number}'s identity stays visible when its long title is shortened:\n{out}")
    shown = [visible(line) for line in out.splitlines()]
    assert any(words[language].search(line) for line in shown), (
        f"#{number}'s current state ({state}) stays visible at {width} cells:\n{out}")
    if state == "closed":
        assert not any(MERGED_WORDS[language].search(line) for line in shown), (
            f"#{number} closed without a merge; shortening must not make it read merged:\n{out}")
    else:
        assert not any(CLOSED_WORDS[language].search(line) for line in shown), (
            f"#{number} merged; its state must stay distinct from a plain close:\n{out}")


# --- The interactive pane --------------------------------------------------------------


class _Tty:
    """A stream that says it is a terminal; writes go to the wrapped stream."""

    def __init__(self, stream):
        self._stream = stream

    def isatty(self):
        return True

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _Screen:
    """A curses window whose size the test changes; it records what each frame draws."""

    def __init__(self, number, sizes):
        self.number = number
        self.sizes = list(sizes)  # [(height, width), ...] in the order the pane takes them
        self.size = self.sizes.pop(0)
        self.y = self.x = 0
        self.frame = []           # [(y, x, text drawn)] since the last refresh
        self.frames = []          # [((height, width), [(y, x, text)])]
        self.content = 0          # frames at this size that show the issue
        self.calls = 0

    def getmaxyx(self):
        return self.size

    def move(self, y, x):
        self.y, self.x = y, x

    def _draw(self, args, limited):
        args = list(args)
        if len(args) >= 3 and isinstance(args[0], int) and isinstance(args[1], int):
            self.y, self.x = args[0], args[1]
            args = args[2:]
        value = args[0]
        value = value.decode() if isinstance(value, bytes) else (chr(value) if isinstance(value, int) else value)
        if limited and len(args) > 1 and isinstance(args[1], int) and args[1] >= 0:
            value = value[:args[1]]
        self.frame.append((self.y, self.x, value))
        self.x += cells(value)

    def addstr(self, *args):
        self._draw(args, False)

    def addnstr(self, *args):
        self._draw(args, True)

    def insstr(self, *args):
        self._draw(args, False)

    def insnstr(self, *args):
        self._draw(args, True)

    def addch(self, *args):
        self._draw(args, False)

    def erase(self):
        self.frame = []

    clear = erase

    def refresh(self):
        self.frames.append((self.size, list(self.frame)))
        if any(visible(text).lstrip().startswith(f"#{self.number} ") and len(visible(text).strip()) > 4
               for _, _, text in self.frame):
            self.content += 1

    noutrefresh = refresh

    def getch(self):
        import curses

        self.calls += 1
        if self.calls > 2000:
            return ord("q")
        if self.content >= 2:
            if not self.sizes:
                return ord("q")
            self.size, self.content = self.sizes.pop(0), 0
            return curses.KEY_RESIZE
        time.sleep(0.01)
        return -1

    get_wch = getch

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _live_pane(main, monkeypatch, number, sizes):
    """`fa dashboard N` in a fake curses pane taking ``sizes`` in turn; the screen recorder."""
    import curses

    screen = _Screen(number, sizes)
    monkeypatch.setattr(sys, "stdin", _Tty(sys.stdin))
    monkeypatch.setattr(sys, "stdout", _Tty(sys.stdout))
    monkeypatch.setattr(curses, "wrapper", lambda draw, *a, **kw: draw(screen, *a, **kw))
    monkeypatch.setattr(curses, "initscr", lambda: screen, raising=False)
    monkeypatch.setattr(curses, "doupdate", lambda: None, raising=False)
    monkeypatch.setattr(curses, "is_term_resized", lambda h, w: (h, w) != screen.size, raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False, raising=False)
    monkeypatch.setattr(curses, "color_pair", lambda n: 0, raising=False)
    for name in ("curs_set", "start_color", "use_default_colors", "init_pair", "noecho", "cbreak",
                 "nocbreak", "echo", "endwin", "mousemask", "mouseinterval", "resizeterm",
                 "resize_term", "napms", "update_lines_cols", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *a, **kw: None, raising=False)
    monkeypatch.setenv("COLUMNS", str(screen.size[1]))
    monkeypatch.setenv("LINES", str(screen.size[0]))
    done = threading.Event()
    result = {}

    def call():
        result["code"] = run_cli(main, ["dashboard", str(number)])
        done.set()

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert done.wait(120), "`fa dashboard N` did not finish in the fake pane"
    return result["code"], screen


def _rows(drawn):
    """Each screen row's drawn text, in column order."""
    found = {}
    for y, x, text in sorted(drawn, key=lambda item: (item[0], item[1])):
        found.setdefault(y, []).append(visible(text))
    return {y: "".join(parts) for y, parts in found.items()}


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("number", [RUNNING, REQUEUED], ids=["live-lane-paths", "finished-run-detail"])
def test_resized_interactive_pane_keeps_rows_within_the_pane(git_history, tmp_path, monkeypatch, capsys,
                                                             language, number):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    code, screen = _live_pane(main, monkeypatch, number, [(30, 80), (30, 40)])
    assert code == 0, "`fa dashboard N` exits 0 after q"

    by_size = {}
    for size, drawn in screen.frames:
        by_size.setdefault(size, []).append(drawn)
    for size in ((30, 80), (30, 40)):
        shown = [drawn for drawn in by_size.get(size, [])
                 if any(visible(text).lstrip().startswith(f"#{number} ") for _, _, text in drawn)]
        assert shown, f"The pane drew issue #{number} at {size[1]} cells: {screen.frames[-3:]}"

    overflow = []
    for (height, width), drawn in screen.frames:
        for y, x, text in drawn:
            if y >= height or x + cells(text) > width:
                overflow.append((width, y, x, cells(text), visible(text)))
    assert not overflow, (
        f"Every drawn row stays inside the pane (cells, wide characters two) while it is "
        f"resized from 80 to 40; outside: {overflow}")

    lane = LANE_TITLES["api" if number == RUNNING else "docs"].split()[0]
    for width in (80, 40):
        final = next(drawn for (height, w), drawn in reversed(screen.frames)
                     if w == width and any(visible(t).lstrip().startswith(f"#{number} ") for _, _, t in drawn))
        rows = list(_rows(final).values())
        assert any(row.lstrip().startswith(f"#{number} ") for row in rows), (
            f"The issue's identity row stays visible at {width} cells: {rows}")
        assert any(lane in row for row in rows), (
            f"The lane row (lane {lane!r}{' and its paths' if number == RUNNING else ''}) stays "
            f"visible at {width} cells: {rows}")
        if number == REQUEUED:
            assert any("DETAIL-7" in row for row in rows), (
                f"The finished run's detail row stays visible at {width} cells: {rows}")

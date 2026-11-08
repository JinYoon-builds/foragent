"""Shared drivers for the click-window acceptance tests.

Two ways to run the real `fa gate watch` live dashboard and click it:

* ``run_terminal`` forks it on a real pseudo-terminal (the pattern of
  tests/helpers/terminal_view.py), types mouse reports in the encoding the dashboard
  itself switched on, and reads the screen back through a small terminal emulator
  that follows every byte the process writes (cursor moves, erases, colours).
* ``run_fake`` runs the same CLI entry point with only the curses screen replaced by
  a recorder (cells, colour pairs, bold), so the same steps run where no pty exists.

A step is a function of the current screen (``Screen``) that returns the next input:
``("click", x, y)`` (zero-based cells) or ``("key", name)`` with name in x, esc, q, s.
The state lives in a temporary folder; the tracker is the in-memory fake, a fake `gh`
on PATH records any call, sockets and executors are forbidden (offline_terminal), no
orchestrator starts, and only the dashboard process the test forked is waited for.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import select
import time
from dataclasses import dataclass, field
from datetime import timedelta

from helpers.cells import cells

COLUMNS, ROWS = 120, 40
BARS = set("▐▌▕▏┃│|▍▎")
COUNT_LINE = re.compile(r"(\d+)\s*개\s*▸\s*누르면\s*목록")
PLANNING_LINE = re.compile(r"기획\s*세션\s*요청\s*(\d+)\s*개\s*▸\s*누르면\s*목록")
STAGE_WORDS = ("구현", "확인", "리뷰", "CI", "차례", "질문", "대기", "막힘", "멈춤")
LANE_NEXT = {"Api": {6, 7, 8}, "Web": {4}, "Review": {2}}
REQUESTS = ("REQ-ALPHA 화면 정리 부탁", "REQ-BETA 문서 갱신 부탁")
PR_NUMBER = 9


# ---- the repository the dashboard reads -----------------------------------------------------

def dashboard_case(git_history, tmp_path, monkeypatch, capsys=None, language="ko", requests=REQUESTS):
    """status_case plus: a live implementing run with a PR (#1, size s), a size-l waiting issue
    (#4 after #1), three next issues in api (#6 #7 #8), two open planning requests, a fake gh;
    then one gate tick publishes the dashboard's now-snapshot."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.store.update_run(1, pid=os.getpid(), phase="implementing",
                          last_activity_at=(case.clock[0] - timedelta(seconds=30)).isoformat())
    case.tracker.add_labels(1, ["size:s"])
    case.tracker.add_labels(4, ["size:l"])
    for number in (6, 7, 8):
        case.tracker.add_issue(number, f"Next task {number}", labels=["approved", "ready", "lane:api"])
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Implement feature",
                                body="Closes #1")
    assert pr == PR_NUMBER, "the fixture's PR number is fixed"
    root = ["--repo-root", str(case.root)]
    for text in requests:
        assert main(["orch", "request", "--from", "planning", "--text", text, *root]) == 0
    if capsys is not None:
        capsys.readouterr()
    rows = json.loads(_cli_out(main, ["orch", "inbox", "--json", *root], capsys))
    for row in rows:
        if row.get("text") in requests:
            assert main(["orch", "request-status", str(row["id"]), "processing", *root]) == 0
    tick(tick_context(case, start_runs=False))
    gh_log = _fake_gh(tmp_path, monkeypatch)
    _guard_tracker(case, monkeypatch, tmp_path / "tracker-calls.log")
    return case, main, gh_log


def _cli_out(main, argv, capsys):
    import contextlib
    import io

    if capsys is not None:
        capsys.readouterr()
        assert main(argv) == 0
        return capsys.readouterr().out
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert main(argv) == 0
    return buffer.getvalue()


def _fake_gh(tmp_path, monkeypatch):
    folder = tmp_path / "fake-bin"
    folder.mkdir(exist_ok=True)
    log = tmp_path / "gh-calls.log"
    script = folder / "gh"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def _guard_tracker(case, monkeypatch, log):
    """After the tick, the dashboard may read only local state: any tracker call is recorded
    in a file (a forked dashboard writes there too)."""
    case.tracker_log = log
    for name in dir(type(case.tracker)):
        if name.startswith("_") or not callable(getattr(type(case.tracker), name, None)):
            continue
        original = getattr(case.tracker, name)

        def record(*args, _name=name, _original=original, **kwargs):
            with open(log, "a", encoding="utf-8") as stream:
                stream.write(_name + "\n")
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, record, raising=False)


def assert_offline(case, gh_log):
    log = case.tracker_log
    assert not log.exists(), f"the dashboard called the GitHub tracker: {log.read_text()}"
    assert not gh_log.exists(), f"the dashboard ran gh: {gh_log.read_text()}"


# ---- what a screen shows --------------------------------------------------------------------

@dataclass
class Screen:
    """One painted screen: text rows by cell and, per cell, (fg, bg, bold) as RGB or None."""

    rows: list[str]
    looks: dict[tuple[int, int], tuple] = field(default_factory=dict)
    chars: dict[tuple[int, int], str] = field(default_factory=dict)

    def text(self) -> str:
        return "\n".join(self.rows)

    def find(self, pattern: str, *, start_row: int = 0) -> list[tuple[int, int]]:
        """(x, y) cells where ``pattern`` (a regex) starts, from ``start_row`` down."""
        found = []
        for y in range(start_row, len(self.rows)):
            for match in re.finditer(pattern, self.rows[y]):
                found.append((cells(self.rows[y][:match.start()]), y))
        return found

    def cell_text(self, y: int, x0: int, x1: int) -> str:
        return "".join(self.chars.get((y, x), "") for x in range(x0, x1))

    def look(self, x: int, y: int) -> tuple:
        return self.looks.get((y, x), (None, None, False))


def bubble_bounds(screen: Screen) -> tuple[int, int]:
    top = next((y for y, row in enumerate(screen.rows) if "╭" in row), None)
    assert top is not None, "the dashboard shows the squirrel's speech bubble:\n" + screen.text()
    bottom = next((y for y in range(top + 1, len(screen.rows)) if "╰" in screen.rows[y]), None)
    assert bottom is not None, "the speech bubble is closed:\n" + screen.text()
    return top, bottom


def bubble(screen: Screen) -> list[str]:
    """The text lines inside the speech bubble, in order, blank lines dropped."""
    top, bottom = bubble_bounds(screen)
    lines = []
    for row in screen.rows[top + 1:bottom]:
        starts = [row.index(mark) for mark in "│┤" if mark in row]
        if not starts or "│" not in row:
            continue
        inner = row[min(starts) + 1:row.rindex("│")].strip()
        if inner:
            lines.append(inner)
    return lines


def acorn_at(screen: Screen, number: int) -> tuple[int, int]:
    """Where the acorn #number is drawn below the bubble (its # cell)."""
    _, bottom = bubble_bounds(screen)
    found = screen.find(rf"#{number}(?!\d)", start_row=bottom + 1)
    assert found, f"acorn #{number} is drawn on the dashboard:\n" + screen.text()
    return found[0]


def acorn_word(screen: Screen, number: int) -> str:
    """The word the acorn carries after its number ('' for a number-only acorn)."""
    x, y = acorn_at(screen, number)
    row = screen.rows[y]
    start = next(i for i in range(len(row)) if cells(row[:i]) == x)
    label = re.match(rf"#{number}\s*([^▌▐\s:]*)", row[start:])
    return label.group(1) if label else ""


def count_lines(screen: Screen) -> list[tuple[int, int, int, str]]:
    """Every 'N개 ▸ 누르면 목록' line below the header: (x, y, N, the row's text)."""
    found = []
    for y, row in enumerate(screen.rows[1:], 1):
        for match in COUNT_LINE.finditer(row):
            found.append((cells(row[:match.start()]), y, int(match.group(1)), row))
    return found


def lane_above(screen: Screen, y: int) -> str | None:
    """The lane title whose name row is nearest above row y."""
    for row in reversed(screen.rows[:y + 1]):
        for title in ("Api", "Web", "Review", "Decision", "Docs"):
            if re.search(rf"🧩 {title}\b", row) and "레인" not in row:
                return title
    return None


def settings_at(screen: Screen) -> tuple[int, int]:
    header = screen.rows[0] if screen.rows else ""
    for word in ("설정", "⚙"):
        if word in header:
            return cells(header[:header.index(word)]), 0
    raise AssertionError("the header shows the settings button:\n" + screen.text())


# ---- xterm colours --------------------------------------------------------------------------

_BASE16 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
           (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
           (255, 0, 255), (0, 255, 255), (255, 255, 255)]


def xterm_rgb(index: int | None):
    if index is None or index < 0:
        return None
    if index < 16:
        return _BASE16[index]
    if index < 232:
        index -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[index // 36], steps[(index // 6) % 6], steps[index % 6]
    level = 8 + (index - 232) * 10
    return level, level, level


def luminance(rgb) -> float:
    if rgb is None:
        return -1.0  # The terminal's own colour: compared as the darkest choice.
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def brighter(after: tuple, before: tuple) -> bool:
    """A cell look is brighter: a lighter background, or the same background with lighter
    text, or bold where it was not."""
    fg_a, bg_a, bold_a = after
    fg_b, bg_b, bold_b = before
    if luminance(bg_a) != luminance(bg_b):
        return luminance(bg_a) > luminance(bg_b)
    if luminance(fg_a) != luminance(fg_b):
        return luminance(fg_a) > luminance(fg_b)
    return bool(bold_a) and not bold_b


# ---- the recorder screen (no pty) -----------------------------------------------------------

class _FakeCurses:
    def __init__(self, curses, steps, height=ROWS, width=COLUMNS):
        self.curses, self.steps = curses, list(steps)
        self.height, self.width = height, width
        self.spans: dict[tuple[int, int], tuple[str, int]] = {}
        self.cursor = (0, 0)
        self.pairs: dict[int, tuple[int, int]] = {0: (-1, -1)}
        self.screens: list[Screen] = []
        self.mouse_event = None

    # curses module functions
    def init_pair(self, index, fg, bg):
        self.pairs[index] = (fg, bg)

    def color_pair(self, index):
        return index << 8

    # screen methods
    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled):
        pass

    def timeout(self, milliseconds):
        pass

    def bkgd(self, *args):
        pass

    def erase(self):
        self.spans.clear()

    def addstr(self, y, x, value, attribute=0):
        end = x + cells(value)
        self.spans = {key: old for key, old in self.spans.items()
                      if key[0] != y or key[1] >= end or key[1] + cells(old[0]) <= x}
        self.spans[y, x] = (value, attribute)

    def move(self, y, x):
        self.cursor = y, x

    def clrtoeol(self):
        y, x = self.cursor
        self.spans = {key: v for key, v in self.spans.items() if key[0] != y or key[1] < x}

    def clrtobot(self):
        y, _ = self.cursor
        self.spans = {key: v for key, v in self.spans.items() if key[0] < y}

    def refresh(self):
        pass

    def snapshot(self) -> Screen:
        chars, looks = {}, {}
        for (y, x), (value, attribute) in sorted(self.spans.items()):
            fg, bg = self.pairs.get((attribute >> 8) & 0xFF, (-1, -1))
            look = (xterm_rgb(fg), xterm_rgb(bg), bool(attribute & self.curses.A_BOLD))
            column = x
            for char in value:
                width = cells(char)
                if width == 0:
                    continue
                chars[y, column] = char
                looks[y, column] = look
                if width == 2:
                    chars[y, column + 1] = ""
                    looks[y, column + 1] = look
                column += width
        rows = []
        for y in range(self.height):
            line, x = "", 0
            limit = max([key[1] for key in chars if key[0] == y], default=-1)
            while x <= limit:
                char = chars.get((y, x))
                line += " " if char is None else char
                x += 1
            rows.append(line.rstrip())
        return Screen(rows, looks, chars)

    def getch(self):
        screen = self.snapshot()
        self.screens.append(screen)
        assert self.steps, "the dashboard asked for more input than the test gave"
        event = self.steps.pop(0)(screen)
        if event[0] == "click":
            _, x, y = event
            self.mouse_event = (0, x, y, 0, self.curses.BUTTON1_PRESSED)
            return self.curses.KEY_MOUSE
        return {"x": ord("x"), "q": ord("q"), "s": ord("s"), "esc": 27}[event[1]]


class _Tty:
    def __init__(self):
        import io

        self.buffer = io.StringIO()

    def isatty(self):
        return True

    def write(self, value):
        return self.buffer.write(value)

    def flush(self):
        pass

    def fileno(self):
        raise OSError("no file descriptor")


def run_fake(main, root, monkeypatch, steps) -> list[Screen]:
    """Run `fa gate watch` with a recorder screen; return the screen seen before each step
    and the last one."""
    import curses
    from foragent import watch

    fake = _FakeCurses(curses, steps)
    monkeypatch.setattr(watch.sys, "stdin", _Tty())
    monkeypatch.setattr(watch.sys, "stdout", _Tty())
    monkeypatch.setattr(curses, "wrapper", lambda callback, *args: callback(fake, *args))
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", fake.init_pair)
    monkeypatch.setattr(curses, "color_pair", fake.color_pair)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 256, raising=False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "set_escdelay", lambda value: None)
    monkeypatch.setattr(curses, "mousemask", lambda mask: (mask, 0))
    monkeypatch.setattr(curses, "mouseinterval", lambda value: None)
    monkeypatch.setattr(curses, "getmouse", lambda: fake.mouse_event)
    assert main(["gate", "watch", "--repo-root", str(root)]) in (0, None)
    return fake.screens


# ---- the real terminal ----------------------------------------------------------------------

class Emulator:
    """Enough of an xterm to follow what curses writes: text by cell width, cursor
    movement, erases, scrolling, insert/delete, SGR colours and the private modes set."""

    def __init__(self, rows=ROWS, columns=COLUMNS):
        self.h, self.w = rows, columns
        self.modes: set[int] = set()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.reset_screen()

    def reset_screen(self):
        self.grid = [[(" ", (None, None, False)) for _ in range(self.w)] for _ in range(self.h)]
        self.y = self.x = 0
        self.wrap = False
        self.top, self.bottom = 0, self.h - 1
        self.fg = self.bg = None
        self.bold = self.reverse = False
        self.saved = (0, 0)

    # -- helpers
    def _look(self):
        fg, bg = (self.bg, self.fg) if self.reverse else (self.fg, self.bg)
        return fg, bg, self.bold

    def _blank(self):
        return (" ", (None, self.bg, False))

    def _clamp(self):
        self.y = max(0, min(self.h - 1, self.y))
        self.x = max(0, min(self.w - 1, self.x))
        self.wrap = False

    def _scroll_up(self, n=1):
        for _ in range(n):
            del self.grid[self.top]
            self.grid.insert(self.bottom, [self._blank() for _ in range(self.w)])

    def _scroll_down(self, n=1):
        for _ in range(n):
            del self.grid[self.bottom]
            self.grid.insert(self.top, [self._blank() for _ in range(self.w)])

    def _index(self):
        if self.y == self.bottom:
            self._scroll_up()
        elif self.y < self.h - 1:
            self.y += 1

    def _put(self, char):
        width = cells(char)
        if width == 0:
            return
        if self.wrap or self.x + width > self.w:
            self.x = 0
            self._index()
            self.wrap = False
        look = self._look()
        self.grid[self.y][self.x] = (char, look)
        if width == 2 and self.x + 1 < self.w:
            self.grid[self.y][self.x + 1] = ("", look)
        if self.x + width >= self.w:
            self.x = self.w - 1
            self.wrap = True
        else:
            self.x += width
        self.last = char

    def _sgr(self, params):
        params = params or [0]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.fg = self.bg = None
                self.bold = self.reverse = False
            elif p == 1:
                self.bold = True
            elif p == 22:
                self.bold = False
            elif p == 7:
                self.reverse = True
            elif p == 27:
                self.reverse = False
            elif 30 <= p <= 37:
                self.fg = xterm_rgb(p - 30)
            elif 90 <= p <= 97:
                self.fg = xterm_rgb(p - 90 + 8)
            elif p == 39:
                self.fg = None
            elif 40 <= p <= 47:
                self.bg = xterm_rgb(p - 40)
            elif 100 <= p <= 107:
                self.bg = xterm_rgb(p - 100 + 8)
            elif p == 49:
                self.bg = None
            elif p in (38, 48) and i + 1 < len(params):
                if params[i + 1] == 5 and i + 2 < len(params):
                    value = xterm_rgb(params[i + 2])
                    i += 2
                elif params[i + 1] == 2 and i + 4 < len(params):
                    value = tuple(params[i + 2:i + 5])
                    i += 4
                else:
                    value = None
                if p == 38:
                    self.fg = value
                else:
                    self.bg = value
            i += 1

    def _csi(self, private, params, final):
        n = (params[0] if params and params[0] else 1)
        if final in "Hf":
            self.y = (params[0] if len(params) > 0 and params[0] else 1) - 1
            self.x = (params[1] if len(params) > 1 and params[1] else 1) - 1
            self._clamp()
        elif final == "A":
            self.y -= n
            self._clamp()
        elif final == "B":
            self.y += n
            self._clamp()
        elif final == "C":
            self.x += n
            self._clamp()
        elif final == "D":
            self.x -= n
            self._clamp()
        elif final == "G" or final == "`":
            self.x = n - 1
            self._clamp()
        elif final == "d":
            self.y = n - 1
            self._clamp()
        elif final == "E":
            self.y += n
            self.x = 0
            self._clamp()
        elif final == "F":
            self.y -= n
            self.x = 0
            self._clamp()
        elif final == "J":
            mode = params[0] if params else 0
            rows = (range(self.y + 1, self.h) if mode == 0 else range(0, self.y) if mode == 1 else range(self.h))
            for row in rows:
                self.grid[row] = [self._blank() for _ in range(self.w)]
            if mode == 0:
                for col in range(self.x, self.w):
                    self.grid[self.y][col] = self._blank()
            elif mode == 1:
                for col in range(0, self.x + 1):
                    self.grid[self.y][col] = self._blank()
            self.wrap = False
        elif final == "K":
            mode = params[0] if params else 0
            cols = range(self.x, self.w) if mode == 0 else range(0, self.x + 1) if mode == 1 else range(self.w)
            for col in cols:
                self.grid[self.y][col] = self._blank()
            self.wrap = False
        elif final == "X":
            for col in range(self.x, min(self.w, self.x + n)):
                self.grid[self.y][col] = self._blank()
        elif final == "P":
            row = self.grid[self.y]
            del row[self.x:self.x + n]
            row.extend(self._blank() for _ in range(self.w - len(row)))
        elif final == "@":
            row = self.grid[self.y]
            for _ in range(n):
                row.insert(self.x, self._blank())
            del row[self.w:]
        elif final == "L":
            if self.top <= self.y <= self.bottom:
                for _ in range(n):
                    del self.grid[self.bottom]
                    self.grid.insert(self.y, [self._blank() for _ in range(self.w)])
        elif final == "M":
            if self.top <= self.y <= self.bottom:
                for _ in range(n):
                    del self.grid[self.y]
                    self.grid.insert(self.bottom, [self._blank() for _ in range(self.w)])
        elif final == "S":
            self._scroll_up(n)
        elif final == "T":
            self._scroll_down(n)
        elif final == "r":
            self.top = (params[0] if len(params) > 0 and params[0] else 1) - 1
            self.bottom = (params[1] if len(params) > 1 and params[1] else self.h) - 1
            self.y = self.x = 0
            self.wrap = False
        elif final == "m" and not private:
            self._sgr(params)
        elif final == "b":
            for _ in range(n):
                self._put(getattr(self, "last", " "))
        elif final in "hl" and private == "?":
            for p in params:
                if final == "h":
                    self.modes.add(p)
                    if p in (1049, 47, 1047):
                        self.reset_screen()
                else:
                    self.modes.discard(p)
        elif final == "s":
            self.saved = (self.y, self.x)
        elif final == "u":
            self.y, self.x = self.saved
            self._clamp()

    def feed(self, data: bytes):
        text = self.pending + self.decoder.decode(data)
        self.pending = ""
        i, size = 0, len(text)
        while i < size:
            char = text[i]
            if char == "\x1b":
                if i + 1 >= size:
                    self.pending = text[i:]
                    return
                nxt = text[i + 1]
                if nxt == "[":
                    match = re.compile(r"\x1b\[([?>=!]?)([0-9;:]*)([ -/]*)([@-~])").match(text, i)
                    if not match:
                        self.pending = text[i:]
                        return
                    params = [int(p) if p.isdigit() else 0 for p in re.split("[;:]", match.group(2))] \
                        if match.group(2) else []
                    if not match.group(3):
                        self._csi(match.group(1), params, match.group(4))
                    i = match.end()
                    continue
                if nxt == "]":
                    end = re.compile(r"\x07|\x1b\\").search(text, i)
                    if not end:
                        self.pending = text[i:]
                        return
                    i = end.end()
                    continue
                if nxt in "()*+":
                    if i + 2 >= size:
                        self.pending = text[i:]
                        return
                    i += 3
                    continue
                if nxt == "7":
                    self.saved = (self.y, self.x)
                elif nxt == "8":
                    self.y, self.x = self.saved
                    self._clamp()
                elif nxt == "M":
                    if self.y == self.top:
                        self._scroll_down()
                    elif self.y > 0:
                        self.y -= 1
                elif nxt == "D":
                    self._index()
                elif nxt == "E":
                    self.x = 0
                    self._index()
                elif nxt == "c":
                    self.reset_screen()
                i += 2
                continue
            if char == "\r":
                self.x = 0
                self.wrap = False
            elif char == "\n":
                self._index()
                self.wrap = False
            elif char == "\b":
                self.x = max(0, self.x - 1)
                self.wrap = False
            elif char == "\t":
                self.x = min(self.w - 1, (self.x // 8 + 1) * 8)
            elif ord(char) < 32 or char == "\x7f":
                pass
            else:
                self._put(char)
            i += 1

    def screen(self) -> Screen:
        rows, looks, chars = [], {}, {}
        for y, row in enumerate(self.grid):
            line = ""
            for x, (char, look) in enumerate(row):
                line += char
                chars[y, x] = char
                looks[y, x] = look
            rows.append(line.rstrip())
        return Screen(rows, looks, chars)


def _mouse_bytes(modes: set[int], x: int, y: int) -> bytes:
    if 1006 in modes:
        return f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode()
    if modes & {1000, 1002, 1003}:
        return bytes([0x1b, ord("["), ord("M"), 32, 33 + x, 33 + y, 0x1b, ord("["), ord("M"), 35, 33 + x, 33 + y])
    raise AssertionError(f"the dashboard never switched on mouse reporting (modes {sorted(modes)})")


KEYS = {"x": b"x", "q": b"q", "s": b"s", "esc": b"\x1b"}


def run_terminal(main, root, steps, *, settle=1.5, first=3.0) -> list[Screen]:
    """Run `fa gate watch` on a 120x40 pty; before each step read the screen, then type the
    step's input. Returns the screens seen before each step and the final one. Ends with q;
    the dashboard is never signalled, only waited for."""
    import fcntl
    import pty
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:
        code = 1
        try:
            import sys
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
            os.environ["COLUMNS"], os.environ["LINES"] = str(COLUMNS), str(ROWS)
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False, buffering=1)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False, buffering=1)
            code = main(["gate", "watch", "--repo-root", str(root)]) or 0
        except BaseException:  # noqa: BLE001  (the parent reports a non-zero exit)
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    emulator, raw = Emulator(), []
    screens, pending = [], list(steps)
    deadline, next_at, error, quit_sent = time.monotonic() + 90, None, None, False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                raw.append(data)
                emulator.feed(data)
            if next_at is None and 1049 in emulator.modes:
                next_at = time.monotonic() + first
            if next_at is not None and time.monotonic() >= next_at:
                screen = emulator.screen()
                screens.append(screen)
                if not pending:
                    break
                try:
                    event = pending.pop(0)(screen)
                except BaseException as exc:  # noqa: BLE001  (reported after the dashboard ends)
                    error, pending = exc, []
                    event = ("key", "q")
                data = (_mouse_bytes(emulator.modes, event[1], event[2]) if event[0] == "click"
                        else KEYS[event[1]])
                os.write(fd, data)
                if event == ("key", "q"):
                    quit_sent, pending = True, []
                    break
                next_at = time.monotonic() + settle
    finally:
        # Only the forked dashboard is stopped, with its own quit key; no signal is sent.
        if not quit_sent:
            try:
                os.write(fd, b"q")
            except OSError:
                pass
        status, waited = None, time.monotonic() + 15
        while status is None and time.monotonic() < waited:
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
            else:
                try:
                    if select.select([fd], [], [], 0.2)[0]:
                        raw.append(os.read(fd, 65536))
                except OSError:
                    time.sleep(0.2)
        os.close(fd)
        if status is None:
            _, status = os.waitpid(pid, 0)  # Closing its terminal ends the dashboard it started.
    if error is not None:
        raise error
    from helpers.terminal_view import _screen_text

    tail = _screen_text(b"".join(raw))[-2000:]
    assert screens and (quit_sent or not pending), "the dashboard did not reach its first frame in time:\n" + tail
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, tail
    return screens


def runner(kind):
    """The session driver for a parametrized test: 'terminal' (real pty) or 'recorder'."""
    if kind == "terminal":
        return lambda main, root, monkeypatch, steps: run_terminal(main, root, steps)
    return run_fake

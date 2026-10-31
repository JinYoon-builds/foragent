"""Acceptance test for wheel-scroll. Locked by Foragent; do not edit.

Drive watch.run, including terminal setup, curses input, painting and Enter.
Reuse the offline snapshot/TTY approach from test_acorn_interact. No watch
input handler, frame builder, viewport, or selection helper is replaced.
"""

from dataclasses import replace
from io import StringIO
import re

import pytest

from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_running import display_cells, running_snapshot, view_offline  # noqa: F401


class Terminal(StringIO):
    """Terminal modes at the output boundary, with alternate-scroll enabled.

    mousemask models ncurses enabling/disabling terminal mouse reporting.
    Explicit DECSET/DECRST output is honored too. A physical wheel is decoded
    as KEY_MOUSE while tracking is active, or as an arrow in the alternate
    screen while alternate-scroll is active. Disabling alternate-scroll alone
    leaves no application input; that must not satisfy the tall-view check.
    """

    def __init__(self):
        super().__init__()
        self.modes = {1007}
        self.curses_mouse = False

    def isatty(self):
        return True

    def write(self, value):
        for match in re.finditer(r"\x1b\[\?([0-9;]+)([hl])", value):
            for number in map(int, match[1].split(";")):
                if match[2] == "h":
                    self.modes.add(number)
                else:
                    self.modes.discard(number)
        return super().write(value)

    def wheel(self, direction, curses, mouse):
        if self.curses_mouse or self.modes.intersection({1000, 1002, 1003}):
            return mouse(direction)
        if self.modes.intersection({47, 1047, 1049}) and 1007 in self.modes:
            return curses.KEY_DOWN if direction == "down" else curses.KEY_UP
        return -1


class Screen:
    """A fake curses screen that keeps the text actually painted at each key."""

    def __init__(self, height, actions, terminal, transport, curses):
        self.height, self.width = height, 100
        self.actions = iter(actions)
        self.terminal, self.transport, self.curses = terminal, transport, curses
        self.spans, self.frames = {}, {}
        self.previous, self.cursor = "initial", (0, 0)
        self.keypad_enabled = False
        self.mouse_event = None
        self.delivered = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled):
        self.keypad_enabled = enabled

    def timeout(self, milliseconds):
        pass

    def erase(self):
        self.spans.clear()

    def addstr(self, y, x, value, attribute=0):
        end = x + display_cells(value)
        self.spans = {key: old for key, old in self.spans.items()
                      if key[0] != y or key[1] >= end or key[1] + display_cells(old) <= x}
        self.spans[y, x] = value

    def move(self, y, x):
        self.cursor = y, x

    def clrtoeol(self):
        y, x = self.cursor
        self.spans = {key: value for key, value in self.spans.items()
                      if key[0] != y or key[1] < x}

    def clrtobot(self):
        y, _ = self.cursor
        self.spans = {key: value for key, value in self.spans.items() if key[0] < y}

    def refresh(self):
        pass

    def mouse(self, direction):
        flag = (self.curses.BUTTON5_PRESSED if direction == "down"
                else self.curses.BUTTON4_PRESSED)
        # Modifier bits must not turn a wheel event into a click or navigation.
        self.mouse_event = (0, 2, 2, 0, flag | self.curses.BUTTON_SHIFT)
        return self.curses.KEY_MOUSE

    def getch(self):
        rows = ["".join(value for (row, _), value in sorted(self.spans.items()) if row == y)
                for y in range(self.height)]
        self.frames[self.previous] = "\n".join(rows)
        name, action = next(self.actions)  # Exhaustion fails instead of hanging.
        self.previous = name
        keys = {"enter": 10, "close": ord("x"), "quit": ord("q"),
                "down": self.curses.KEY_DOWN, "up": self.curses.KEY_UP}
        if action.startswith("wheel-"):
            direction = action.removeprefix("wheel-")
            key = (self.mouse(direction) if self.transport == "key-mouse"
                   else self.terminal.wheel(direction, self.curses, self.mouse))
        else:
            key = keys[action]
        self.delivered.append((action, key))
        return key


def opened_number(frame):
    """Read the issue's unique Goal text from the real Enter popup."""
    numbers = re.findall(r"LOCKED-ACORN-(\d+)", frame)
    assert len(numbers) == 1, f"Enter must open exactly one acorn's details:\n{frame}"
    return int(numbers[0])


def first_line(frame):
    match = re.search(r"\blines (\d+)[–-]\d+ of \d+\b", frame)
    assert match, f"the tall view must expose its scroll position:\n{frame}"
    return int(match[1])


def run_session(tmp_path, monkeypatch, height, transport):
    import curses
    from foragent import watch
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    snapshot, config = running_snapshot()
    slugs = ("work", "queue", "human", "blocked")
    config = replace(config, animation=False, view_glyphs="ascii", language="en",
                     lanes={slug: config.lanes[slug] for slug in slugs})
    snapshot = replace(snapshot, completed=0, total=4, order=None,
                       done={slug: [] for slug in slugs}, events=[], requests=[],
                       status={**snapshot.status, "lanes": {
                           slug: snapshot.status["lanes"][slug] for slug in slugs}})
    tracker, store = FakeTracker(), StateStore(tmp_path / "state")
    for number, slug in zip(range(101, 105), slugs):
        issue = tracker.add_issue(number, f"Task {number}", labels=[f"lane:{slug}"],
                                  body=f"## Goal\nLOCKED-ACORN-{number}\n")
        store.record_issue(issue, config.lanes)

    # Supply a coherent five-button curses API even on macOS builds that omit
    # BUTTON5_PRESSED. This tests the consumer's behavior, not that platform's ABI.
    for name, flag in {"BUTTON4_PRESSED": 1 << 16, "BUTTON5_PRESSED": 1 << 21,
                       "BUTTON_SHIFT": 1 << 26, "ALL_MOUSE_EVENTS": (1 << 28) - 1}.items():
        monkeypatch.setattr(curses, name, flag, raising=False)
    terminal = Terminal()

    def mousemask(mask):
        terminal.curses_mouse = bool(mask)
        return mask, 0

    actions = [("baseline-open", "enter"), ("baseline-close", "close")]
    wheel_actions = ("wheel-down", "wheel-down", "wheel-up", "wheel-up", "wheel-up")
    for index, action in enumerate(wheel_actions):
        actions.extend([(f"wheel-{index}", action),
                        (f"wheel-open-{index}", "enter"), (f"wheel-close-{index}", "close")])
    for direction in ("down", "up"):
        actions.extend([(f"arrow-{direction}", direction),
                        (f"arrow-open-{direction}", "enter"),
                        (f"arrow-close-{direction}", "close")])
    actions.append(("quit", "quit"))
    screen = Screen(height, actions, terminal, transport, curses)
    monkeypatch.setattr(watch.sys, "stdin", Terminal())
    monkeypatch.setattr(watch.sys, "stdout", terminal)
    monkeypatch.setattr(curses, "wrapper", lambda callback, *args: callback(screen, *args))
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "set_escdelay", lambda value: None)
    monkeypatch.setattr(curses, "mousemask", mousemask)
    monkeypatch.setattr(curses, "mouseinterval", lambda value: None)
    monkeypatch.setattr(curses, "getmouse", lambda: screen.mouse_event)
    # Only the external data boundary is fixed; the full watch UI runs unchanged.
    monkeypatch.setattr(watch, "collect", lambda *args, **kwargs: snapshot)
    watch.run(lambda: snapshot.status, tracker, store, config, tmp_path,
              now=lambda: snapshot.captured_at)
    return screen


@pytest.mark.parametrize("transport", ["terminal", "key-mouse"])
@pytest.mark.parametrize("height", [12, 80], ids=["tall-content", "fits"])
def test_wheel_scroll_preserves_acorn_and_arrows_still_choose(
    tmp_path, monkeypatch, height, transport,
):
    screen = run_session(tmp_path, monkeypatch, height, transport)
    frames = screen.frames
    selected = opened_number(frames["baseline-open"])
    assert selected == 101, "the first fixture acorn is initially selected"
    if height == 12:
        # Two down notches, two up notches, then another up at the top. Exact
        # deltas detect a skipped, reversed or doubled scroll, not just motion.
        start = first_line(frames["baseline-close"])
        actual = [first_line(frames[f"wheel-{index}"]) for index in range(5)]
        assert actual == [start + 1, start + 2, start + 1, start, start], (
            f"each wheel notch must scroll one row and clamp at the top: {actual}; "
            f"delivered inputs: {screen.delivered}"
        )
    else:
        assert "lines " not in frames["baseline-close"], "fixture content must fit"
        for index in range(5):
            assert frames[f"wheel-{index}"] == frames["baseline-close"], (
                "a wheel must leave a fitting view unchanged"
            )
    chosen = [opened_number(frames[f"wheel-open-{index}"]) for index in range(5)]
    assert chosen == [selected] * 5, f"wheel changed acorn selection: {selected} -> {chosen}"
    down = opened_number(frames["arrow-open-down"])
    up = opened_number(frames["arrow-open-up"])
    assert down == 102, "Down must choose the next acorn once, including when content fits"
    assert up == selected, "Up must undo one Down selection, without skipping or doubling it"

"""Acceptance tests for click-window: clicks reach the dashboard and explain their target
in the squirrel's speech bubble.

Each statement runs twice through the public `fa gate watch` entry point after one gate
tick published the now-snapshot:

* ``terminal``: the real dashboard on a pseudo-terminal; the test types the mouse report
  the dashboard switched on (SGR 1006 or X10) at the cell where the target is painted and
  reads the screen back through a terminal emulator (colours included).
* ``recorder``: the same CLI with only the curses screen replaced by a recorder, so the
  statements can also be checked where the machine has no pty devices.

Fixture (click_window_helpers.dashboard_case, language ko): #1 working (implementing,
size s, open PR #9), #2 blocked, #3 waits for the person's answer, #4 waits on #1 (size l),
#6 #7 #8 are the api lane's next work, two planning-session requests are open. A fake gh
on PATH and a recording tracker prove no GitHub call happens on redraw or click.
"""

import re

import pytest

from helpers.terminal_view import offline_terminal, isolated_home  # noqa: F401  (autouse, offline)
from click_window_helpers import (BARS, LANE_NEXT, PLANNING_LINE, PR_NUMBER, REQUESTS, STAGE_WORDS, acorn_at, acorn_word,
                                  assert_offline, brighter, bubble, bubble_bounds, count_lines,
                                  dashboard_case, lane_above, runner, settings_at)

RUNNERS = ["terminal", "recorder"]
TITLES = {1: "Implement feature", 3: "Choose locale", 4: "Login page"}
LANES = {1: "Api", 3: "Decision", 4: "Web"}
SIZES = {1: "s", 3: "m", 4: "l"}
URL = "https://github.com/owner/repo/issues/{}"


@pytest.fixture
def dash(git_history, tmp_path, monkeypatch, capsys):
    return dashboard_case(git_history, tmp_path, monkeypatch, capsys)


class Script:
    """One session's inputs, planned from the first screen; keeps every screen it saw."""

    def __init__(self, plan):
        self.plan, self.actions, self.screens = plan, None, []

    def __call__(self, screen):
        self.screens.append(screen)
        if self.actions is None:
            self.actions = list(self.plan(screen)) + [("key", "q")]
        action = self.actions.pop(0)
        return action(screen) if callable(action) else action

    def steps(self, count=40):
        return [self] * count


def click_acorn(number):
    return lambda screen: ("click", *acorn_at(screen, number))


def run(kind, dash, monkeypatch, plan):
    case, main, gh_log = dash
    script = Script(plan)
    runner(kind)(main, case.root, monkeypatch, script.steps())
    assert_offline(case, gh_log)
    return script.screens


def joined(screen):
    return " ".join(bubble(screen))


def person_name():
    from foragent import watch

    return watch.text("question_person", "ko")


def _size_tokens(text):
    """Standalone size letters (s, m, l in either case) in the bubble text."""
    return {token.lower() for token in re.findall(r"(?<![A-Za-z])([SMLsml])(?![A-Za-z])", text)}


def _explanation_ok(screen, before, number):
    lines = bubble(screen)
    text = " ".join(lines)
    assert f"#{number} {TITLES[number]}" in text, f"the bubble names #{number} and its title: {lines}"
    word = acorn_word(before, number)
    if word:
        assert word in text, f"the bubble repeats the acorn's state word {word!r}: {lines}"
    else:
        assert any(stage in text for stage in STAGE_WORDS), f"the bubble says #{number}'s state: {lines}"
    if number == 3:
        assert person_name() in text, f"a question's bubble says it is the person's turn: {lines}"
    else:
        assert person_name() not in text, f"machine work does not name the person: {lines}"
    if number == 4:
        rest = text.replace(f"#{number} {TITLES[number]}", "")
        assert re.search(r"#1(?!\d)", rest), f"#4's reason names what it waits for (#1): {lines}"
    assert LANES[number] in text, f"the bubble names the lane: {lines}"
    plain = " ".join(line for line in lines if "github.com" not in line and TITLES[number] not in line)
    assert _size_tokens(plain.replace("PR", "").replace("CI", "")) == {SIZES[number]}, (
        f"the bubble shows #{number}'s size {SIZES[number]!r} and no other: {lines}")
    if number == 1:
        assert re.search(r"\d+\s*(초|분|시간)", text), f"a working issue's bubble shows its elapsed time: {lines}"
    assert URL.format(number) in text, f"the bubble carries the issue link: {lines}"
    assert "PR" in text, f"the bubble has a PR row (the PR or none): {lines}"
    has_pr = re.search(rf"PR\D{{0,8}}{PR_NUMBER}(?!\d)", text)
    if number == 1:
        assert has_pr, f"#1's bubble names its open PR #{PR_NUMBER}: {lines}"
    else:
        assert not has_pr and not re.search(rf"#{PR_NUMBER}(?!\d)", text), (
            f"#{number} has no PR, so its bubble says none: {lines}")


# ---- statement 1: a left click on an acorn explains its issue in the bubble -------------------

@pytest.mark.parametrize("kind", RUNNERS)
def test_left_click_on_an_acorn_explains_its_issue_in_the_bubble(dash, monkeypatch, kind):
    screens = run(kind, dash, monkeypatch, lambda first: [click_acorn(1), click_acorn(3), click_acorn(4)])
    first, after_1, after_3, after_4 = screens[:4]
    assert TITLES[1] not in joined(first), "before any click the bubble shows its default lines"
    _explanation_ok(after_1, first, 1)
    _explanation_ok(after_3, first, 3)
    _explanation_ok(after_4, first, 4)


# ---- statement 2: a click on a "N개 ▸ 누르면 목록" line lists those items -----------------------

def _listing_ok(row, count, lane, listed, default):
    text = " ".join(listed)
    assert listed != default, f"a click on {row.strip()!r} changes the bubble"
    if "기획" in row:
        found = [subject for subject in REQUESTS if subject.split()[0] in text]
        assert found == list(REQUESTS), f"the bubble lists the open planning requests: {listed}"
        assert all(text.count(subject.split()[0]) == 1 for subject in REQUESTS), f"each once: {listed}"
        assert count == len(REQUESTS), f"the line's count matches the list: {row.strip()!r}"
        return
    assert lane in LANE_NEXT, f"a next-work count line belongs to a lane with next work: {row.strip()!r}"
    numbers = [int(n) for n in re.findall(r"#(\d+)", text)]
    assert set(numbers) == LANE_NEXT[lane], f"the bubble lists {lane}'s next work {LANE_NEXT[lane]}: {listed}"
    assert len(numbers) == len(set(numbers)), f"each item is listed once: {listed}"
    assert count == len(LANE_NEXT[lane]), f"the line's count matches the list: {row.strip()!r}"


@pytest.mark.parametrize("kind", RUNNERS)
def test_click_on_a_count_line_lists_its_items_in_the_bubble(dash, monkeypatch, kind):
    seen = {}

    def plan(first):
        lines = count_lines(first)
        seen["lines"] = lines
        actions = []
        for x, y, _, _ in lines:
            actions += [("click", x, y), ("click", x, y)]  # open the list, then close it again
        return actions

    screens = run(kind, dash, monkeypatch, plan)
    first, lines = screens[0], seen["lines"]
    assert lines, "the dashboard shows at least one 'N개 ▸ 누르면 목록' line:\n" + first.text()
    default = bubble(first)
    for index, (x, y, count, row) in enumerate(lines):
        listed, closed = bubble(screens[1 + 2 * index]), bubble(screens[2 + 2 * index])
        _listing_ok(row, count, lane_above(first, y), listed, default)
        assert closed == default, f"clicking {row.strip()!r} again returns the default lines: {closed}"


# ---- statement 3: the same click again, x or Esc returns the default lines ---------------------
# The new part of this statement is closing a "N개 ▸ 누르면 목록" list (red on main: main draws no
# such line). Closing an acorn explanation already works on main; that regression guard lives in
# test_dash_click_window_guard.py (outside Done when), not here.

def _target(first, which):
    if which == "acorn":
        return acorn_at(first, 4)
    lines = count_lines(first)
    assert lines, "the dashboard shows a 'N개 ▸ 누르면 목록' line:\n" + first.text()
    return lines[0][:2]


def _close_plan(which, close):
    def plan(first):
        x, y = _target(first, which)
        return [("click", x, y), ("click", x, y) if close == "same" else ("key", close)]
    return plan


def _closed_ok(screens, which, close):
    first, opened, closed = screens[:3]
    assert bubble(opened) != bubble(first), f"clicking the {which} changes the bubble"
    assert bubble(closed) == bubble(first), f"{close} returns the bubble to its default lines: {bubble(closed)}"
    assert "이 컴퓨터" not in closed.text(), "closing an explanation opens nothing else"


@pytest.mark.parametrize("close", ["same", "x", "esc"])
def test_same_click_x_or_esc_closes_a_count_line_list(dash, monkeypatch, close):
    _closed_ok(run("recorder", dash, monkeypatch, _close_plan("list", close)), "list", close)


def _terminal_rounds(dash, monkeypatch, which):
    def plan(first):
        x, y = _target(first, which)
        return [("click", x, y), ("click", x, y), ("click", x, y), ("key", "x"), ("click", x, y), ("key", "esc")]

    screens = run("terminal", dash, monkeypatch, plan)
    default = bubble(screens[0])
    for index, close in enumerate(("same", "x", "esc")):
        opened, closed = screens[1 + 2 * index], screens[2 + 2 * index]
        assert bubble(opened) != default, f"clicking the {which} changes the bubble ({close} round)"
        assert bubble(closed) == default, f"{close} returns the bubble to its default lines after the {which}"


def test_real_terminal_same_click_x_or_esc_closes_a_count_line_list(dash, monkeypatch):
    _terminal_rounds(dash, monkeypatch, "list")


# ---- statement 4: the selected target is drawn brighter, with a bar on each side -------------

def _cell_x(screen, y, x):
    """String index of cell x on row y."""
    from helpers.cells import cells

    row = screen.rows[y]
    return next(i for i in range(len(row) + 1) if cells(row[:i]) >= x)


def _bars(screen, number):
    """(bars left of #N, bars right of its label): the bars in the acorn's own glyph run, plus
    a standalone run of bars one space away (never a neighbouring acorn's cap)."""
    x, y = acorn_at(screen, number)
    row = screen.rows[y]
    at = _cell_x(screen, y, x)
    left, i = 0, at - 1
    while i >= 0 and row[i] != " ":
        left += row[i] in BARS
        i -= 1
    if i >= 1 and row[i - 1] in BARS:
        k = i - 1
        while k >= 0 and row[k] in BARS:
            k -= 1
        if k < 0 or row[k] == " ":
            left += i - 1 - k
    label = re.match(rf"#{number}[^▐▌▕▏┃│|▍▎]*", row[at:])
    j, right = at + label.end(), 0
    while j < len(row) and row[j] != " ":
        right += row[j] in BARS
        j += 1
    if j + 1 < len(row) and row[j + 1] in BARS:
        k = j + 1
        while k < len(row) and row[k] in BARS:
            k += 1
        if k >= len(row) or row[k] == " ":
            right += k - j - 1
    return left, right


def _selection_ok(before, selected, after, number, other):
    x0, y0 = acorn_at(before, number)
    x1, y1 = acorn_at(selected, number)
    assert brighter(selected.look(x1, y1), before.look(x0, y0)), (
        f"the selected acorn #{number} is drawn brighter: {before.look(x0, y0)} -> {selected.look(x1, y1)}")
    (l0, r0), (l1, r1) = _bars(before, number), _bars(selected, number)
    assert l1 > l0 and r1 > r0, (
        f"the selected acorn #{number} gets a bar on each side: {before.rows[y0]!r} -> {selected.rows[y1]!r}")
    xo0, yo0 = acorn_at(before, other)
    xo1, yo1 = acorn_at(selected, other)
    assert selected.look(xo1, yo1) == before.look(xo0, yo0) and _bars(selected, other) == _bars(before, other), (
        f"only the selected acorn changes; #{other} stays as it was: {selected.rows[yo1]!r}")
    x2, y2 = acorn_at(after, number)
    assert after.look(x2, y2) == before.look(x0, y0) and _bars(after, number) == (l0, r0), (
        f"closing the selection draws #{number} as before: {after.rows[y2]!r}")


@pytest.mark.parametrize("kind", RUNNERS)
def test_selected_acorn_is_drawn_brighter_with_a_bar_on_each_side(dash, monkeypatch, kind):
    screens = run(kind, dash, monkeypatch,
                  lambda first: [click_acorn(6), click_acorn(6), click_acorn(4), ("key", "x")])
    first, six, six_closed, four, four_closed = screens[:5]
    _selection_ok(first, six, six_closed, 6, 8)
    _selection_ok(six_closed, four, four_closed, 4, 8)


# ---- statement 5: click-window itself draws the planning-request count line ------------------

def _planning_lines(screen):
    """(x, y, N, row) of every '기획 세션 요청 N개 ▸ 누르면 목록' line on the screen."""
    from helpers.cells import cells

    found = []
    for y, row in enumerate(screen.rows):
        for match in PLANNING_LINE.finditer(row):
            found.append((cells(row[:match.start()]), y, int(match.group(1)), row))
    return found


@pytest.mark.parametrize("kind", RUNNERS)
def test_bubble_draws_the_planning_request_count_line_and_lists_it_on_click(dash, monkeypatch, kind):
    def plan(first):
        lines = _planning_lines(first)
        return [("click", x, y) for x, y, _, _ in lines[:1] for _ in range(2)]

    screens = run(kind, dash, monkeypatch, plan)
    first = screens[0]
    lines = _planning_lines(first)
    assert len(lines) == 1, (
        "with two open planning-session requests the dashboard draws exactly one "
        "'기획 세션 요청 N개 ▸ 누르면 목록' line:\n" + first.text())
    x, y, count, row = lines[0]
    top, bottom = bubble_bounds(first)
    assert top < y < bottom, f"the planning-request line is inside the squirrel's speech bubble: {row!r}"
    assert count == len(REQUESTS), f"the line counts the {len(REQUESTS)} open requests: {row.strip()!r}"
    assert len(COUNTED.findall(" ".join(bubble(first)))) == 1, f"the line is drawn once: {bubble(first)}"
    default = bubble(first)
    listed, closed = bubble(screens[1]), bubble(screens[2])
    _listing_ok(row, count, None, listed, default)
    assert closed == default, f"clicking the planning-request line again returns the default lines: {closed}"


COUNTED = re.compile(r"기획\s*세션\s*요청\s*\d+\s*개")

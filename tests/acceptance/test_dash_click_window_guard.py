"""Regression guards for click-window that already hold on main (not Done when).

They keep today's behaviour while click-window lands: closing an acorn explanation by the
same click, x or Esc; the settings button opening settings; a click on nothing changing
nothing; and no planning-request count line when no request is open. The statements' red
evidence is in test_dash_click_window.py.
"""

import pytest

from helpers.terminal_view import offline_terminal, isolated_home  # noqa: F401  (autouse, offline)
from click_window_helpers import assert_offline, bubble, bubble_bounds, dashboard_case, runner, settings_at
from test_dash_click_window import (COUNTED, RUNNERS, TITLES, Script, _close_plan, _closed_ok,  # noqa: F401
                                    _terminal_rounds, click_acorn, dash, joined, run)


@pytest.mark.parametrize("close", ["same", "x", "esc"])
def test_regression_same_click_x_or_esc_closes_an_acorn_explanation(dash, monkeypatch, close):
    """Already holds on main (regression guard, not red evidence)."""
    _closed_ok(run("recorder", dash, monkeypatch, _close_plan("acorn", close)), "acorn", close)


def test_regression_real_terminal_same_click_x_or_esc_closes_an_acorn_explanation(dash, monkeypatch):
    """Already holds on main (regression guard, not red evidence)."""
    _terminal_rounds(dash, monkeypatch, "acorn")


# ---- regression guards: the settings button (not a behaviour statement of this issue) ----------
# A left click on the settings button already opens settings on main (recorder and
# pseudo-terminal; also with an acorn explanation open). Per the PM's answer this was dropped
# from click-window's statements; these guards only keep it working, and the Orca terminal
# check stays a person checklist item. They are not red evidence for any statement.

@pytest.mark.parametrize("kind", RUNNERS)
def test_regression_left_click_on_the_settings_button_opens_settings(dash, monkeypatch, kind):
    """Already holds on main (regression guard, not red evidence)."""
    first, opened = run(kind, dash, monkeypatch, lambda first: [("click", *settings_at(first))])[:2]
    assert "이 컴퓨터" not in first.text(), "the settings window is closed before the click"
    assert all(label in opened.text() for label in ("팀", "이 컴퓨터")), (
        "a left click on the settings button opens the settings window:\n" + opened.text())


@pytest.mark.parametrize("kind", RUNNERS)
def test_regression_settings_click_opens_settings_while_an_acorn_is_explained(dash, monkeypatch, kind):
    """Already holds on main (regression guard, not red evidence)."""
    screens = run(kind, dash, monkeypatch, lambda first: [click_acorn(1), lambda s: ("click", *settings_at(s))])
    assert TITLES[1] in joined(screens[1]), "the acorn's explanation is open before the settings click"
    assert all(label in screens[2].text() for label in ("팀", "이 컴퓨터")), (
        "the settings button opens settings while an explanation is open:\n" + screens[2].text())


# ---- regression guard from the goal: clicks on nothing change nothing ------------------------

@pytest.mark.parametrize("kind", RUNNERS)
def test_regression_a_click_on_nothing_changes_nothing(dash, monkeypatch, kind):
    """Already holds on main (goal guard, not red evidence for a statement)."""
    def blank(first):
        _, bottom = bubble_bounds(first)
        y = next(y for y in range(bottom + 1, len(first.rows) - 1) if not first.rows[y].strip())
        return [("click", 60, y)]

    first, after = run(kind, dash, monkeypatch, blank)[:2]
    assert bubble(after) == bubble(first), "a click on an empty cell leaves the bubble as it was"
    assert "이 컴퓨터" not in after.text(), "a click on an empty cell opens nothing"


def test_regression_no_planning_request_line_without_open_requests(git_history, tmp_path, monkeypatch, capsys):
    """Counterpart of statement 5 (N > 0 only); already holds on main, so it is a guard
    against a doubled or unconditional line, not red evidence."""
    case, main, gh_log = dashboard_case(git_history, tmp_path, monkeypatch, capsys, requests=())
    script = Script(lambda first: [])
    runner("recorder")(main, case.root, monkeypatch, script.steps())
    assert_offline(case, gh_log)
    first = script.screens[0]
    bubble(first)  # the bubble is drawn
    assert not COUNTED.search(first.text()), (
        "with no open planning-session request the bubble draws no request count line:\n" + first.text())

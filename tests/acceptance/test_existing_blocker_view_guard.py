"""Regression controls for view-shows-existing-blocker (outside Done when; pass on main).

- The dashboard statement already holds on main: the clicked acorn's bubble names the
  existing issue it waits for, and the main screen names it nowhere (D112), all redrawn
  from local state with GitHub unreachable. These controls keep that while the brief changes.
- Plan-key style dependencies and unblocked work keep their current view: the brief's lane
  representative still names its blocker, and work with no blocker names no wait.
"""

import re

import pytest

from existing_blocker_view_world import (
    EXISTING, UNBLOCKED, WAITER, any_waiting_for, brief_lines, build, dashboard_opened, issue_line, waiting_for,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_running import utf8_terminal, view_offline  # noqa: F401


def named(number: int, frame: str) -> int:
    return len(re.findall(rf"#{number}(?!\d)", frame))


# ------------------------------------------------ the dashboard (already on main)

@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_bubble_names_the_existing_issue_it_waits_for(
        git_history, tmp_path, monkeypatch, capsys, language):
    case, main = build(git_history, tmp_path, monkeypatch, language)
    before, opened = dashboard_opened(main, case, capsys, monkeypatch, WAITER)  # asserts no tracker call
    assert named(EXISTING, before) == 0, f"D112: the main screen names no blocker:\n{before}"
    assert named(EXISTING, opened) == 1, f"#{WAITER}'s bubble names #{EXISTING} once:\n{opened}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_bubble_names_a_running_existing_blocker(
        git_history, tmp_path, monkeypatch, capsys, language):
    case, main = build(git_history, tmp_path, monkeypatch, language, blocker=1)
    before, opened = dashboard_opened(main, case, capsys, monkeypatch, WAITER)
    # #1 is itself on screen as a running acorn: the click adds exactly one mention.
    assert named(1, opened) - named(1, before) == 1, f"#{WAITER}'s bubble names #1 once:\n{opened}"
    assert named(EXISTING, opened) == 0, "only the recorded blocker is named"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_names_the_blocker_only_on_the_waiting_issue(
        git_history, tmp_path, monkeypatch, capsys, language):
    case, main = build(git_history, tmp_path, monkeypatch, language)
    _before, (waiter, free) = dashboard_opened(main, case, capsys, monkeypatch, (WAITER, UNBLOCKED))
    assert named(EXISTING, waiter) == 1, waiter
    assert named(EXISTING, free) == 0, f"#{UNBLOCKED} has no blocker:\n{free}"


# ------------------------------------------------ plan-key / unblocked controls

def test_brief_still_names_the_blocker_of_a_lanes_only_queued_issue_in_korean(
        git_history, tmp_path, monkeypatch, capsys):
    case, main = build(git_history, tmp_path, monkeypatch, "ko", blocker=1, with_unblocked=False)
    lines = brief_lines(main, case, capsys)
    waiter = issue_line(lines, WAITER)
    assert len(waiter) == 1 and len(waiting_for(1, "ko").findall(waiter[0])) == 1, lines


def test_brief_still_names_the_blocker_number_of_a_lanes_only_queued_issue_in_english(
        git_history, tmp_path, monkeypatch, capsys):
    case, main = build(git_history, tmp_path, monkeypatch, "en", blocker=1, with_unblocked=False)
    lines = brief_lines(main, case, capsys)
    waiter = issue_line(lines, WAITER)
    assert len(waiter) == 1, lines
    # main says "(waiting for 1)"; the change may add '#', but the blocker stays named once.
    assert len(re.findall(r"\bwait(?:ing|s)?\s+(?:for|on)\s+#?1(?!\d)", waiter[0], re.IGNORECASE)) == 1, waiter
    assert not waiting_for(EXISTING, "en").search("\n".join(lines))


@pytest.mark.parametrize("language", ["en", "ko"])
def test_unblocked_work_names_no_wait(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = build(git_history, tmp_path, monkeypatch, language)
    free = issue_line(brief_lines(main, case, capsys), UNBLOCKED)
    assert len(free) == 1 and not any_waiting_for(language).search(free[0]), free
    _before, opened = dashboard_opened(main, case, capsys, monkeypatch, UNBLOCKED)
    assert not any_waiting_for(language).search(opened), opened


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_redraw_of_a_waiting_issue_reads_no_github(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = build(git_history, tmp_path, monkeypatch, language)
    _before, opened = dashboard_opened(main, case, capsys, monkeypatch, WAITER)  # asserts no tracker call
    assert f"#{WAITER}" in opened

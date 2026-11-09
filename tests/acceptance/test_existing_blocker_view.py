"""Jin r1301: an approved issue waiting on an existing issue #N is shown as waiting for #N
on the dashboard and in `fa brief`, from local state, with no GitHub call on redraw.

World: existing_blocker_view_world (gate tick records the issues; #41 waits on existing #40).
"""

import re

import pytest

from existing_blocker_view_world import (
    EXISTING, UNBLOCKED, WAITER, any_waiting_for, brief_lines, build, issue_line, waiting_for,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_running import utf8_terminal, view_offline  # noqa: F401

LANGUAGES = ["en", "ko"]


# Statement 1 (the dashboard) already holds on current main: clicking the waiting acorn
# opens a bubble that names the existing blocker ("waiting its turn · after #40"). Its
# regression controls live in test_existing_blocker_view_guard.py, outside Done when.


# ------------------------------------------------ statement 2: fa brief

@pytest.mark.parametrize("language", LANGUAGES)
def test_brief_names_the_issue_waiting_for_the_existing_issue(
        git_history, tmp_path, monkeypatch, capsys, language):
    # #39 is listed first in the web lane, so #41 is not the lane's representative issue.
    case, main = build(git_history, tmp_path, monkeypatch, language)
    lines = brief_lines(main, case, capsys)
    waiter = issue_line(lines, WAITER)
    assert len(waiter) == 1, f"fa brief lists #{WAITER} once: {lines}"
    found = waiting_for(EXISTING, language).findall(waiter[0])
    assert len(found) == 1, f"#{WAITER}'s brief line says once that it waits for #{EXISTING}: {waiter[0]!r}"
    assert len(re.findall(rf"#{EXISTING}(?!\d)", waiter[0])) == 1, f"#{EXISTING} is named once: {waiter[0]!r}"
    free = issue_line(lines, UNBLOCKED)
    assert len(free) == 1 and not any_waiting_for(language).search(free[0]), (
        f"#{UNBLOCKED} has no blocker, so its brief line names no wait: {free}")


# A lane's only queued issue already names its blocker number on main (en "(waiting for 1)",
# ko "(#1 기다리는 중)"): only the '#' would differ, so those cases are regression controls in
# test_existing_blocker_view_guard.py, not Done when tests.

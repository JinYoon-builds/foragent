"""Regression guard for issue-view-width (UX r1212); not part of Done when (passes on main).

Wider terminals look as today: at 120 columns `fa dashboard N --once` still prints the whole
title and the whole issue link on its own line.
"""

import pytest

from narrow_issue_view import LONG_TITLES, QUEUED, REPO, REQUEUED, RUNNING, show, world


@pytest.mark.parametrize("language", ["en", "ko"])
def test_wide_terminal_keeps_whole_title_and_link(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    title = LONG_TITLES[language]
    for number in (RUNNING, QUEUED, REQUEUED):
        out = show(main, capsys, monkeypatch, number, 120)
        assert f"#{number} {title}" in out, f"At 120 columns the title of #{number} is whole:\n{out}"
    for number in (RUNNING, QUEUED):
        out = show(main, capsys, monkeypatch, number, 120)
        url = f"https://github.com/{REPO}/issues/{number}"
        assert url in [line.strip() for line in out.splitlines()], (
            f"At 120 columns the link of #{number} is whole on its own line:\n{out}")

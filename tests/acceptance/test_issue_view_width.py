"""Acceptance test for issue-view-width (UX r1212). Locked by Foragent; do not edit.

The owner reads one issue in a phone-width (40-column) Orca pane. On main, `fa dashboard N
--once` printed a title wider than the pane (#2482, a queued issue whose last run was held),
a lane's path list on one 130-column line (#2672) and the issue link cut into a wrong URL
('https://github.com/JinYoon-builds/forage').

Real CLI, gate tick and local state; only GitHub and the clock are fake. The terminal width
comes from COLUMNS, which `--once` reads like any terminal program. Width is display width:
Korean and emoji count as two columns.
"""

import pytest

from narrow_issue_view import (LONG_TITLES, QUEUED, REPO, REQUEUED, RUNNING, cells, queued_numbers,
                               show, world)

WIDTH = 40
ELLIPSES = ("…", "...")


def _title_kept(out, number, title):
    """The title wraps (all of it is there) or is shortened with an ellipsis, never just dropped."""
    squeezed = "".join(title.split())
    if squeezed in "".join(out.split()):
        return True
    for line in out.splitlines():
        line = line.rstrip()
        if not line.startswith(f"#{number} "):
            continue
        for mark in ELLIPSES:
            if line.endswith(mark):
                shown = "".join(line[len(f"#{number} "):-len(mark)].split())
                if shown and squeezed.startswith(shown):
                    return True
    return False


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("number", [RUNNING, QUEUED, REQUEUED], ids=["running", "queued", "queued-after-held-run"])
def test_every_line_of_the_issue_view_fits_40_columns(git_history, tmp_path, monkeypatch, capsys,
                                                      language, number):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    assert case.store.get_run(RUNNING).status == "running", "Setup: #1 is running"
    assert {QUEUED, REQUEUED} <= queued_numbers(main, capsys), (
        "Setup: the main dashboard shows #4 and #5 as queued")

    out = show(main, capsys, monkeypatch, number, WIDTH)
    wide = [(cells(line), line) for line in out.splitlines() if cells(line) > WIDTH]
    assert not wide, (
        f"At {WIDTH} columns every line of `fa dashboard {number} --once` must fit "
        f"(display width, wide characters count two); too wide: {wide}\n{out}")
    assert _title_kept(out, number, LONG_TITLES[language]), (
        f"A long title wraps or is shortened with an ellipsis; it is not left out:\n{out}")


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("number", [RUNNING, QUEUED], ids=["running", "queued"])
def test_issue_link_is_whole_or_absent_at_40_columns(git_history, tmp_path, monkeypatch, capsys,
                                                    language, number):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    url = f"https://github.com/{REPO}/issues/{number}"
    wide = show(main, capsys, monkeypatch, number, 120)
    assert url in wide.splitlines(), f"Setup: a wide terminal shows the whole link on its own line:\n{wide}"

    out = show(main, capsys, monkeypatch, number, WIDTH)
    lines = [line.strip() for line in out.splitlines()]
    linked = [line for line in lines if "https://" in line or "github.com" in line]
    assert all(line == url for line in linked), (
        f"At {WIDTH} columns the link {url} prints whole on its own line or not at all, "
        f"never cut: {linked}\n{out}")
    cut = [line for line in lines if len(line) >= len("https://") and url.startswith(line) and line != url]
    assert not cut, f"A cut prefix of {url} is a wrong URL: {cut}\n{out}"
    assert len(linked) <= 1, f"The link prints at most once: {linked}\n{out}"

"""Acceptance test for dash-one-row-per-issue. Locked by Foragent; do not edit.

Jin 2026-11-05 (screenshot of the lane view): the same issue number appeared in several
places on the main screen, so nobody could tell how many issues were actually running.
D112: the main screen shows only what is happening now.

- An issue that is running appears exactly once on the main screen, under its lane.
- The main screen states how many issues are running now and how many wait, matching
  `fa gate status`.

Layout and wording are the lane's (#2078 owns the main-screen layout). The checks read
the printed `fa gate watch --once` screen (run with COLUMNS at several widths)
by issue numbers, lane titles and counts only, never by fixed words.
"""

import json
import re
import unicodedata

import pytest

# Lane titles that occur nowhere else on the screen, so a title marks its lane block.
TITLES = {"api": "Quokka", "web": "Pelican", "docs": "Walrus"}
RUNNING = {1: "api", 5: "api", 8: "api"}
WIDTHS = (80, 120, 160)


def _case(git_history, tmp_path, monkeypatch, language):
    """Three running issues in lane api, four waiting their turn (none held, none asking).

    #1 and #5 run from the snapshot; #8 is started by the gate after the tick that wrote
    the snapshot, as the gate does (refresh first, then start). #6 waits for #1 in api,
    #4 waits for #5 in web, #7 and #9 are approved in web.
    """
    from foragent.scheduler import tick
    from foragent.tracker import Dependency
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    lanes = {slug: {"paths": [f"{slug}/**"], "title": title, "emoji": "🧩"}
             for slug, title in TITLES.items()}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="reviewing")
    case.tracker.add_issue(5, "Second api", labels=["approved", "running", "lane:api"])
    case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5), lane="api")
    case.store.update_run(5, phase="checking")
    case.tracker.add_issue(6, "Third api", labels=["approved", "lane:api"])
    case.tracker.set_blocked_by(6, [Dependency(1, "open")])
    case.tracker.add_issue(8, "Fourth api", labels=["approved", "ready", "lane:api"])
    case.tracker.add_issue(4, "Login page", labels=["approved", "lane:web"])
    case.tracker.set_blocked_by(4, [Dependency(5, "open")])
    case.tracker.add_issue(7, "Web two", labels=["approved", "ready", "lane:web"])
    case.tracker.add_issue(9, "Web three", labels=["approved", "lane:web"])
    tick(tick_context(case, start_runs=False))
    case.store.start_run(8, branch="foragent/issue-8", worktree=case.store.worktree_path(8), lane="api")
    case.store.update_run(8, phase="implementing")
    return case, main


def _screens(main, case, capsys):
    """`fa gate watch --once` as printed, once per terminal width given through COLUMNS.

    Only the public command is used: an implementation may size the printed screen from
    the terminal or keep one fixed width, and either way every printed screen is checked.
    """
    screens = {}
    for width in WIDTHS:
        capsys.readouterr()
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("COLUMNS", str(width))
            assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
        screens[f"COLUMNS={width}"] = capsys.readouterr().out
    return screens


def _gate_summary(main, capsys, case):
    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)["summary"]


def _plain(text):
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _cells(text):
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def _places(screen, pattern):
    """(row, cell column) of every match on the plain screen."""
    places = []
    for row, line in enumerate(_plain(screen).splitlines()):
        for match in re.finditer(pattern, line):
            places.append((row, _cells(line[:match.start()])))
    return places


def _issue(number):
    return rf"#{number}(?!\d)"


def _lane_of(screen, place):
    """The lane whose title is the nearest one at or above the place (left of it on its row)."""
    row, column = place
    titles = [(title_row, title_column, slug)
              for slug, title in TITLES.items()
              for title_row, title_column in _places(screen, re.escape(title))
              if (title_row, title_column) <= (row, column)]
    assert titles, f"no lane title above the issue at row {row}"
    # Nearest row first; on that row the title closest to the left of the issue.
    best = max(titles, key=lambda found: (found[0], found[1] if found[1] <= column else -1))
    return best[2]


def _counts(screen):
    """Every number the screen states, issue numbers (#N) and percentages left out."""
    plain = re.sub(r"#\d+", " ", _plain(screen))
    return [int(found) for found in re.findall(r"(?<![\d.:])\d+(?![\d%:])", plain)]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_each_running_issue_appears_once_under_its_lane(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = _case(git_history, tmp_path, monkeypatch, language)
    summary = _gate_summary(main, capsys, case)
    assert summary["working"] == len(RUNNING), f"fixture: the gate counts every running issue: {summary}"
    for name, screen in _screens(main, case, capsys).items():
        for number, lane in RUNNING.items():
            places = _places(screen, _issue(number))
            assert len(places) == 1, (
                f"{name}: running issue #{number} must appear exactly once on the main screen, "
                f"found {len(places)}:\n{_plain(screen)}")
            assert _lane_of(screen, places[0]) == lane, (
                f"{name}: running issue #{number} must be under its lane {TITLES[lane]}:\n{_plain(screen)}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_main_screen_counts_running_and_waiting_like_gate_status(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = _case(git_history, tmp_path, monkeypatch, language)
    summary = _gate_summary(main, capsys, case)
    # Fixture guard: nothing is held or asks a person, so "wait" is the gate's queued count.
    assert (summary["working"], summary["queued"], summary["blocked"], summary["waiting_human"]) == (3, 4, 0, 0), summary
    for name, screen in _screens(main, case, capsys).items():
        stated = _counts(screen)
        assert summary["working"] in stated, (
            f"{name}: the main screen must say {summary['working']} issues run now, as fa gate status does; "
            f"numbers on screen {stated}:\n{_plain(screen)}")
        assert summary["queued"] in stated, (
            f"{name}: the main screen must say {summary['queued']} issues wait, as fa gate status does; "
            f"numbers on screen {stated}:\n{_plain(screen)}")

    # The counts follow the gate: start one more waiting issue (#9) and refresh.
    case.store.start_run(9, branch="foragent/issue-9", worktree=case.store.worktree_path(9), lane="web")
    case.store.update_run(9, phase="implementing")
    tick(tick_context(case, start_runs=False))
    after = _gate_summary(main, capsys, case)
    assert (after["working"], after["queued"]) == (4, 3), after
    for name, screen in _screens(main, case, capsys).items():
        stated = _counts(screen)
        assert after["working"] in stated and after["queued"] in stated, (
            f"{name}: after one more issue starts, the screen must say {after['working']} run and "
            f"{after['queued']} wait, as fa gate status does; numbers on screen {stated}:\n{_plain(screen)}")

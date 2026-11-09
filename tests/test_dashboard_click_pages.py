"""The squirrel's bubble never drops what a click opened: contents longer than one page of six
rows go on the next pages, and the page line shows them (review class: required facts
discarded on overflow; inaccessible list overflow)."""

import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from helpers.terminal_view import isolated_home, offline_terminal  # noqa: E402,F401  (autouse, offline)
from click_window_helpers import dashboard_case  # noqa: E402

PAGE = re.compile(r"(\d+)/(\d+)")


@pytest.fixture(autouse=True)
def utf8(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")


def _frame(case, state, width):
    from foragent import watch, watch_now

    layout = {}
    now = case.clock[0]
    snapshot = watch_now.read(case.store, now, case.config.language)
    frame = watch.render_frame(snapshot, case.config, case.root, width=width, now=now, state=state,
                               layout=layout)
    return frame.splitlines(), layout


def _bubble(lines):
    top = next(i for i, row in enumerate(lines) if "╭" in row)
    bottom = next(i for i in range(top + 1, len(lines)) if "╰" in lines[i])
    body = []
    for row in lines[top + 1:bottom]:
        start = min(row.index(mark) for mark in "│┤" if mark in row)
        body.append(row[start + 1:row.rindex("│")].strip())
    return lines[bottom], [line for line in body if line]


def _click(case, layout, state, target):
    from foragent import watch

    x0, y0, *_ = next(hit for hit in layout["hits"] if hit[4] == target)
    watch.handle_mouse("left", x0, y0, layout, case.store, case.config, state=state)


def _every_page(case, state, width, opened):
    """The bubble's lines on each page, turning pages with the page line until it comes back."""
    pages = []
    lines, layout = _frame(case, state, width)
    edge, body = _bubble(lines)
    pages.append(body)
    if "▸" not in edge:
        return pages
    count = int(PAGE.search(edge).group(2))
    for _ in range(count):
        _click(case, layout, state, ("page", None))
        assert state.get("explanation_target") == opened, "turning the page keeps the explanation open"
        lines, layout = _frame(case, state, width)
        pages.append(_bubble(lines)[1])
    assert pages[-1] == pages[0], "after the last page the first one comes back"
    return pages[:-1]


def test_narrow_english_acorn_keeps_turn_size_and_pr_on_a_later_page(git_history, tmp_path, monkeypatch):
    case, _main, _gh = dashboard_case(git_history, tmp_path, monkeypatch, language="en")
    # A quiet worker adds its warning row; the issue has a Goal, a step time, size s and PR #9.
    case.store.update_run(1, last_activity_at=(case.clock[0] - timedelta(minutes=45)).isoformat())
    state = {"store": case.store}
    _, layout = _frame(case, state, 80)
    _click(case, layout, state, ("acorn", 1))
    pages = _every_page(case, state, 80, ("acorn", 1))
    assert len(pages) > 1, f"at 80 columns the explanation needs more than one page: {pages}"
    text = " ".join(line for page in pages for line in page)
    for fact in ("#1 Implement feature", "Implement the feature.", "machine work", "size s", "PR #9",
                 "in this step", "https://github.com/owner/repo/issues/1", "Api"):
        assert text.count(fact) == 1, f"{fact!r} is shown exactly once across the pages: {pages}"
    assert all(len(page) <= 6 for page in pages), pages
    assert len(pages[0]) == 6, "the first page uses all six rows"


REQUESTS = tuple(f"REQ-{n} subject {n}" for n in range(1, 9))


def test_eight_planning_requests_are_all_reachable_through_the_page_line(git_history, tmp_path, monkeypatch):
    case, _main, _gh = dashboard_case(git_history, tmp_path, monkeypatch, language="en", requests=REQUESTS)
    state = {"store": case.store}
    lines, layout = _frame(case, state, 120)
    assert any("Planning-session requests 8" in line for line in _bubble(lines)[1])
    _click(case, layout, state, ("list", "requests"))
    pages = _every_page(case, state, 120, ("list", "requests"))
    listed = [token for page in pages for line in page for token in re.findall(r"REQ-\d+", line)]
    assert listed == [subject.split()[0] for subject in REQUESTS], f"each request once, in order: {pages}"
    # A click on a listed row (not the page line) closes the list again.
    lines, layout = _frame(case, state, 120)
    _click(case, layout, state, ("list", "requests"))
    assert state.get("explanation_target") is None
    assert not any("REQ-1 " in line for line in _bubble(_frame(case, state, 120)[0])[1])

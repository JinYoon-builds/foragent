"""Acceptance tests for member-card: a squirrel opens its lane's member card.

Statements:
1. Clicking a lane squirrel shows, in the speech bubble, the lane's owned paths, executor and
   model, fixed skills, recently used skills, today's merges, review rejections and reverts,
   its current issue with minutes, the latest lesson from its lane memory, and its memory folder.
2. A lane with no current issue, no history or no memory still shows every row of the card,
   with an unknown or none value.

The card is found without fixing its layout: the dashboard's own snapshot (`fa gate watch
--once`) is drawn, the squirrel's public hit box is clicked through handle_mouse, and the
speech bubble's body rows (however tall it grew) are the card. The real-terminal check runs `fa gate watch` in a
pseudo-terminal and clicks with real mouse bytes. No GitHub call is allowed on click.
"""

import os
import re
import time

import pytest

from helpers.terminal_view import isolated_home, offline_terminal  # noqa: F401  (offline pty, HOME)
from member_card_support import (
    FIXED_SKILLS, FOLDER, MODEL, NEW_LESSON, NONE_WORDS, OLD_LESSON, OUTSIDE_LESSON, PATHS,
    QUIET_PATHS, USED_SKILL, card_text, clicked_session, dashboard_card, member_world, numbers,
)
from test_local_truth import record_gh

MEMORY_FOLDER = FOLDER.split("/", 1)[1]  # "agent-memory/fa-api": a cut path still ends with it
LESSON_MARK = "quokka-77"
MIN_ROWS = 8  # paths, executor+model, fixed skills, used skills, today, current issue, lesson, folder
REVERTS = re.compile(r"(?i)revert")


@pytest.fixture(autouse=True)
def utf8_terminal(monkeypatch):
    """The bubble is drawn with its rounded box (a UTF-8 terminal, not the ASCII fallback)."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


@pytest.fixture(autouse=True)
def restored_zone():
    old = os.environ.get("TZ")
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _world(git_history, tmp_path, monkeypatch, name, **options):
    folder = tmp_path / name
    folder.mkdir()
    return member_world(git_history, folder, monkeypatch, **options)


def _card(git_history, tmp_path, monkeypatch, capsys, lane="api", name="world", **options):
    case, main = _world(git_history, tmp_path, monkeypatch, name, **options)
    gh = record_gh(monkeypatch)
    before, _, rows, _ = dashboard_card(case, main, capsys, lane)
    assert gh == [], "opening the member card reads only local facts: no GitHub call"
    shown = card_text(before)
    assert MODEL not in shown and USED_SKILL not in shown, (
        f"the bubble shows the card only after the click:\n{shown}")
    return rows


def _has_number(text, value):
    return re.search(rf"(?<![\d#])0*{value}(?!\d)", text) is not None


def test_squirrel_click_opens_a_member_card_with_every_lane_fact(git_history, tmp_path, monkeypatch, capsys):
    rows = _card(git_history, tmp_path, monkeypatch, capsys)
    card = card_text(rows)
    assert len(rows) >= MIN_ROWS, f"the bubble grows to a row for each card fact:\n{card}"
    for path in PATHS:
        assert path in card, f"owned path {path} is on the card:\n{card}"
    assert "claude" in card.lower() and MODEL in card, f"executor and model come from the lane setting:\n{card}"
    for skill in FIXED_SKILLS:
        assert skill in card, f"fixed skill {skill} comes from the lane setting:\n{card}"
    assert USED_SKILL in card, f"skills read in the lane's recorded runs are on the card:\n{card}"
    assert re.search(r"#1(?!\d)", card) and _has_number(card, 37), (
        f"the current issue #1 shows with its 37 minutes:\n{card}")
    assert LESSON_MARK in card and "walrus" not in card, (
        f"only the latest lesson of the lane memory ({NEW_LESSON!r}) is shown, not {OLD_LESSON!r}:\n{card}")
    assert MEMORY_FOLDER in card, f"the lane's memory folder {FOLDER} is on the card:\n{card}"
    assert REVERTS.search(card), f"today's reverts have their place on the card:\n{card}"
    assert card.count(MODEL) == 1 and card.count(LESSON_MARK) == 1, f"each fact is shown once:\n{card}"
    for other in QUIET_PATHS:
        assert other not in card, "the card shows only the clicked lane"


def test_card_counts_todays_merges_in_local_time(git_history, tmp_path, monkeypatch, capsys):
    # Today in Seoul: #11 (05:00 UTC) and #12 (15:30 UTC the day before); #13 merged yesterday.
    base = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="base"))
    more = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="more", extra_merge=True))
    gained, lost = numbers(more) - numbers(base), numbers(base) - numbers(more)
    assert gained["3"] >= 1 and lost["2"] >= 1, (
        "two merges today in local time become three with one more today's merge "
        f"(yesterday's #13 never counts):\nbefore:\n{base}\nafter:\n{more}")


def test_card_counts_todays_review_rejections_in_local_time(git_history, tmp_path, monkeypatch, capsys):
    # Today in Seoul: one FAIL (#11); the FAIL of #13 was yesterday in local time.
    base = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="base"))
    more = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="more", extra_rejection=True))
    gained, lost = numbers(more) - numbers(base), numbers(base) - numbers(more)
    assert gained["2"] >= 1 and lost["1"] >= 1, (
        "one review rejection today in local time becomes two with one more today's FAIL:\n"
        f"before:\n{base}\nafter:\n{more}")


def test_card_current_issue_minutes_follow_the_run(git_history, tmp_path, monkeypatch, capsys):
    early = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="early", minutes=37))
    later = card_text(_card(git_history, tmp_path, monkeypatch, capsys, name="later", minutes=52))
    assert _has_number(early, 37) and not _has_number(early, 52), early
    assert _has_number(later, 52) and not _has_number(later, 37), (
        f"the current issue's minutes are measured from its run start:\n{later}")


def test_lane_with_no_issue_history_or_memory_keeps_every_card_row(git_history, tmp_path, monkeypatch, capsys):
    case, main = _world(git_history, tmp_path, monkeypatch, "world")
    gh = record_gh(monkeypatch)
    _, _, full, _ = dashboard_card(case, main, capsys, "api")
    _, _, empty, _ = dashboard_card(case, main, capsys, "quiet")
    assert gh == []
    text = card_text(empty)
    assert len(full) >= MIN_ROWS and len(empty) >= MIN_ROWS, (
        f"an empty lane keeps every card row:\nfull:\n{card_text(full)}\nempty:\n{text}")
    assert REVERTS.search(text), f"the reverts row stays for an empty lane:\n{text}"
    assert QUIET_PATHS[0] in text and "codex" in text.lower(), f"the configured facts are still shown:\n{text}"
    for leaked in (MODEL, *FIXED_SKILLS, USED_SKILL, LESSON_MARK, MEMORY_FOLDER, PATHS[0]):
        assert leaked not in text, f"{leaked!r} belongs to another lane:\n{text}"
    assert not re.search(r"#1(?!\d)", text), f"the quiet lane has no current issue:\n{text}"
    unknown = [row for _, row in empty if NONE_WORDS.search(row)]
    assert len(unknown) >= 4, (
        "fixed skills, used skills, current issue and lesson read as unknown or none, "
        f"rather than disappearing:\n{text}")


def test_lane_without_current_issue_keeps_the_issue_row(git_history, tmp_path, monkeypatch, capsys):
    working = _card(git_history, tmp_path, monkeypatch, capsys, name="working")
    idle = _card(git_history, tmp_path, monkeypatch, capsys, name="idle", current=False)
    text = card_text(idle)
    assert len(working) >= MIN_ROWS and len(idle) >= MIN_ROWS, (
        f"the card keeps its rows when the lane has no current issue:\n{text}")
    assert MODEL in text and USED_SKILL in text and LESSON_MARK in text, text
    assert not re.search(r"#1(?!\d)", text), f"a stopped run is not the current issue:\n{text}"
    assert any(NONE_WORDS.search(row) for _, row in idle), f"the current issue reads none:\n{text}"


def test_memory_missing_or_outside_the_repository_reads_unknown(git_history, tmp_path, monkeypatch, capsys):
    normal = _card(git_history, tmp_path, monkeypatch, capsys, name="normal")
    missing = _card(git_history, tmp_path, monkeypatch, capsys, name="missing", memory="missing")
    outside = _card(git_history, tmp_path, monkeypatch, capsys, name="outside", memory="outside")
    for name, rows in (("missing", missing), ("outside", outside)):
        text = card_text(rows)
        assert len(normal) >= MIN_ROWS and len(rows) >= MIN_ROWS, (
            f"{name} memory keeps the lesson and folder rows:\n{text}")
        assert LESSON_MARK not in text and "marmot" not in text, (
            f"{name} memory shows no lesson ({OUTSIDE_LESSON!r} is outside the repository):\n{text}")
        assert MODEL in text and USED_SKILL in text, f"other facts are unaffected:\n{text}"
        assert any(NONE_WORDS.search(row) for _, row in rows), f"the lesson reads unknown or none:\n{text}"


def test_real_terminal_squirrel_click_shows_the_member_card(git_history, tmp_path, monkeypatch):
    case, main = _world(git_history, tmp_path, monkeypatch, "world")
    gh = record_gh(monkeypatch)  # inherited by the dashboard child: a gh call fails its exit status
    drawn = clicked_session(main, case.root, ("squirrel", "api"), tmp_path / "hits.json")
    assert gh == []
    for value in (MODEL, FIXED_SKILLS[0], USED_SKILL, LESSON_MARK):
        assert value in drawn, (
            f"a real mouse click on the api squirrel draws its member card ({value!r} missing):\n{drawn[-2000:]}")

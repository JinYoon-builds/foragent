"""Acceptance test for dash-review-words. Locked by Foragent; do not edit.

Jin 2026-11-05: the dashboard showed '리뷰 6번 8번' and nobody could tell what the numbers
meant. A review stage on the screen says which round it is in words (for example
'리뷰 6번째'), and never shows two bare round numbers side by side.

The round in progress is one more than the reviewer rounds already recorded for the
issue: each finished round is journaled (`kind: review`, `round`) and saved as
`runs/issue-<n>/review-<round>.txt`, the same records the runner itself numbers from.
"""

import re
from datetime import timedelta

import pytest

KO_ORDINAL = {1: r"(?:1\s*번째|첫\s*번째)"}
EN_ORDINAL = {1: r"(?:round\s*1\b|\b1st\b|\bfirst\b)"}


def _ko(round_number):
    return KO_ORDINAL.get(round_number, rf"(?<!\d){round_number}\s*번째")


def _en(round_number):
    return EN_ORDINAL.get(round_number, rf"(?:round\s*{round_number}\b|(?<!\d){round_number}th\b)")


def _record_rounds(case, issue, rounds, verdict="FAIL"):
    from foragent import orch_events

    folder = case.store.root / "runs" / f"issue-{issue}"
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, rounds + 1):
        at = case.clock[0] - timedelta(minutes=60 - number)
        orch_events.append_event(case.store, at, issue, "review", f"{verdict} 2 findings",
                                 verdict=verdict, findings=2, round=number, source="reviewer")
        (folder / f"review-{number}.txt").write_text(f"VERDICT: {verdict}\n", encoding="utf-8")


def _screens(git_history, tmp_path, monkeypatch, capsys, language, rounds, other_rounds=0):
    """`fa gate watch --once` (the main screen) and the same screen after a click on #1.

    Issue 1 is in its review stage; issue 2 (another lane) has its own, larger round
    history, so a number taken from the wrong issue shows."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_squirrel_running import plain_frame
    from test_stall_visibility import _dashboard
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    tick(tick_context(case, start_runs=False))
    assert case.store.get_run(1).phase == "reviewing"
    _record_rounds(case, 1, rounds)
    if other_rounds:
        _record_rounds(case, 2, other_rounds)
    printed, opened = _dashboard(main, case, capsys)
    return plain_frame(printed), plain_frame(opened)


def _bare_pairs(frame):
    """Two bare round-like numbers next to each other ('6번 8번', '6 8', '6회 8회')."""
    found = []
    for line in frame.splitlines():
        tokens = re.sub(r"[│╭╮╰╯┤├─◖◗█▌▐▀▄░]", " ", line).split()
        for left, right in zip(tokens, tokens[1:]):
            if re.fullmatch(r"\d+(?:번|회)?", left) and re.fullmatch(r"\d+(?:번|회)?", right):
                found.append(line.strip())
    return found


def _unexplained(line, pattern):
    """Bare numbers left on the review stage once its round words are taken out."""
    rest = re.sub(pattern, " ", line, flags=re.IGNORECASE)
    # A number with its unit ('11 min', '3 h', '2 findings') says what it counts.
    rest = re.sub(r"(?<![\w#])\d+\s+(?=[^\W\d_])(?!번|회)", " ", rest)
    tokens = re.sub(r"[│╭╮╰╯┤├─◖◗█▌▐▀▄░·|]", " ", rest).split()
    return [token for token in tokens if re.fullmatch(r"\d+(?:번|회)?", token)]


def _review_lines(frame, word):
    return [line for line in frame.splitlines() if word in line.lower()]


@pytest.mark.parametrize("rounds", [5, 1])
def test_korean_review_stage_says_its_round_in_words(git_history, tmp_path, monkeypatch, capsys, rounds):
    current = rounds + 1
    printed, opened = _screens(git_history, tmp_path, monkeypatch, capsys, "ko", rounds, other_rounds=7)
    pattern = _ko(current)
    lines = [line for line in _review_lines(printed, "리뷰") if re.search(pattern, line)]
    assert len(lines) == 1, (
        f"the main screen's review stage for #1 says round {current} in words "
        f"(e.g. '리뷰 {current}번째') exactly once:\n{printed}")
    assert len(re.findall(pattern, lines[0])) == 1, f"the round is said once, not repeated: {lines[0]!r}"
    assert not _unexplained(lines[0], pattern), f"no unexplained number beside the round: {lines[0]!r}"
    stray = [n for n in re.findall(r"(?<!\d)(\d+)\s*번째", printed) if int(n) != current]
    assert not stray, f"only #1's current round {current} is shown, not {stray}:\n{printed}"
    for frame in (printed, opened):
        assert not _bare_pairs(frame), f"no two bare round numbers side by side: {_bare_pairs(frame)}"


@pytest.mark.parametrize("rounds", [5, 0])
def test_english_review_stage_says_its_round_in_words(git_history, tmp_path, monkeypatch, capsys, rounds):
    current = rounds + 1
    printed, opened = _screens(git_history, tmp_path, monkeypatch, capsys, "en", rounds, other_rounds=7)
    pattern = _en(current)
    lines = [line for line in _review_lines(printed, "review")
             if re.search(pattern, line, flags=re.IGNORECASE)]
    assert len(lines) == 1, (
        f"the main screen's review stage for #1 says round {current} in words "
        f"(e.g. 'review round {current}') exactly once:\n{printed}")
    assert len(re.findall(pattern, lines[0], flags=re.IGNORECASE)) == 1, (
        f"the round is said once, not repeated: {lines[0]!r}")
    assert not _unexplained(lines[0], pattern), f"no unexplained number beside the round: {lines[0]!r}"
    stray = [n for n in re.findall(r"round\s*(\d+)|(?<!\d)(\d+)(?:st|nd|rd|th)\b", printed, flags=re.IGNORECASE)
             if int(n[0] or n[1]) != current]
    assert not stray, f"only #1's current round {current} is shown, not {stray}:\n{printed}"
    for frame in (printed, opened):
        assert not _bare_pairs(frame), f"no two bare round numbers side by side: {_bare_pairs(frame)}"


def test_bare_pair_detector_catches_the_screenshot_text():
    """The detector the tests above rely on flags the 11-05 screenshot text and spares words."""
    assert _bare_pairs("◖█#1 리뷰 6번 8번▌")
    assert _bare_pairs("#1 review 6 8")
    assert not _bare_pairs("◖█#1 리뷰 6번째▌ ○ 멈춘 듯 11분")
    assert not _bare_pairs("#1 review round 6 · 2 min")
    assert _unexplained("◖█#1 리뷰 6번째 8번▌", _ko(6)) == ["8번"]
    assert _unexplained("◖█#1 review round 6 8▌", _en(6)) == ["8"]
    assert not _unexplained("◖█#1 리뷰 6번째▌ ○ 멈춘 듯 11분", _ko(6))
    assert not _unexplained("◖█#1 review round 6▌ ○ looks stalled 11 min", _en(6))

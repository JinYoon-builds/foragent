"""Acceptance test for short-acorn: a reviewing acorn shows only its stage word.

Jin's rule (order 2026-11-07 dashboard click rest): one word and color on the acorn, details
on click. The review round that #2323 wrote on the acorn ('#1 리뷰 6번째') moves to the click
bubble: the main screen's acorn reads '#1 리뷰' / '#1 review', and a click on it says the
round in words inside the squirrel's speech bubble.

This is the corrected form of the locked tests/acceptance/test_dash_review_words.py: the same
records (journaled reviewer rounds and saved review-<k>.txt files, a second issue with a
larger history) and the same `fa gate watch --once` + click path, but the round is read from
the bubble instead of the acorn. Nothing here calls GitHub or starts a process.
"""

import re

import pytest

from test_dash_review_words import _bare_pairs, _en, _ko, _screens


def _labels(frame, number):
    return [m.strip() for m in re.findall(rf"#{number}(?!\d)([^▌\n]*)▌", frame)]


def _outside_acorns(frame):
    """The frame with every sideways acorn ('#N word▌') taken out."""
    return re.sub(r"#\d+[^▌\n]*▌", " ", frame)


CASES = [("ko", 5, "리뷰"), ("ko", 1, "리뷰"), ("en", 5, "review"), ("en", 0, "review")]


@pytest.mark.parametrize("language,rounds,word", CASES)
def test_reviewing_acorn_shows_only_its_stage_word(git_history, tmp_path, monkeypatch, capsys,
                                                   language, rounds, word):
    printed, opened = _screens(git_history, tmp_path, monkeypatch, capsys, language, rounds,
                               other_rounds=7)
    current = rounds + 1
    pattern = _ko(current) if language == "ko" else _en(current)
    for name, frame in (("main screen", printed), ("clicked screen", opened)):
        labels = _labels(frame, 1)
        assert labels, f"{name}: acorn #1 is drawn:\n{frame}"
        for label in labels:
            assert label.lower() == word, (
                f"{name}: the reviewing acorn reads '#1 {word}' with no round, got '#1 {label}'")
    assert not re.search(pattern, printed, flags=re.IGNORECASE), (
        f"the round is a click detail; the unclicked screen does not say it:\n{printed}")


@pytest.mark.parametrize("language,rounds", [("ko", 5), ("ko", 1), ("en", 5), ("en", 0)])
def test_click_bubble_says_the_review_round_in_words(git_history, tmp_path, monkeypatch, capsys,
                                                     language, rounds):
    printed, opened = _screens(git_history, tmp_path, monkeypatch, capsys, language, rounds,
                               other_rounds=7)
    current = rounds + 1
    pattern = _ko(current) if language == "ko" else _en(current)
    rest = _outside_acorns(opened)
    found = re.findall(pattern, rest, flags=re.IGNORECASE)
    assert len(found) == 1, (
        f"a click on #1 says its round {current} in words once in the bubble "
        f"(e.g. '리뷰 {current}번째' / 'review round {current}'), found {found}:\n{opened}")
    lines = [line for line in rest.splitlines() if re.search(pattern, line, flags=re.IGNORECASE)]
    assert "│" in lines[0] or "|" in lines[0], f"the round sits inside the speech bubble: {lines[0]!r}"
    if language == "ko":
        stray = [n for n in re.findall(r"(?<!\d)(\d+)\s*번째", opened) if int(n) != current]
    else:
        stray = [m[0] or m[1] for m in re.findall(r"round\s*(\d+)|(?<!\d)(\d+)(?:st|nd|rd|th)\b",
                                                  opened, flags=re.IGNORECASE)
                 if int(m[0] or m[1]) != current]
    assert not stray, f"only #1's current round {current} is said, not #2's {stray}:\n{opened}"
    for frame in (printed, opened):
        assert not _bare_pairs(frame), f"no two bare round numbers side by side: {_bare_pairs(frame)}"

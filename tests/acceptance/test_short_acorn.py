"""Acceptance test for short-acorn (order 2026-11-07 dashboard click rest).

Jin 2026-11-07: acorns that read '#2158 질문: 진님' or '#2352 대기: 오케스트레이터' are too
long, and lane rows repeat file paths and every next acorn. After this change:

- an acorn's label is its issue number and one word from the stage vocabulary
  구현/확인/리뷰/CI/차례/질문/대기/막힘/멈춤 (English equivalents in English), with no person,
  PM or role name on it; the word tells a person's turn (질문) from machine work;
- lane rows show no file paths (they move to the member card);
- a lane with next work shows it as one count line, '다음 N개 ▸ 누르면 목록', instead of
  listing the next acorns.

Frames come from the public renderer (`watch.render_frame`) and from `fa dashboard --once`,
which draws the gate's now-snapshot. Nothing here calls GitHub or starts a process.

Every case here is red on main f3c65a33. Acorns main already draws right (machine waits
that read 대기, a reviewing acorn without a round, English stage words, a lane with nothing
next) are not separate cases: they are walked inside red cases, so the change cannot fix one
acorn by breaking another.
"""

from dataclasses import replace
import re

import pytest

from test_acorn_interact import hit_positions
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)
from test_squirrel_running import plain_frame, single_worker, utf8_terminal, view_offline  # noqa: F401

VOCABULARY_KO = {"구현", "확인", "리뷰", "CI", "차례", "질문", "대기", "막힘", "멈춤"}
MACHINE_KO = VOCABULARY_KO - {"질문"}
# Names that must never ride on an acorn: the person (configured or the old hardcoded one),
# the PM and every role whose turn an acorn used to name.
NAMES = ("진님", "Mina", "PM", "오케스트레이터", "머지 담당", "orchestrator", "merge owner",
         "you", "architect", "docs owner")
PERSON = "Mina"
QUEUED = (103, 104, 105, 106, 107)
PATHS = ("src/zz_unique_view/**", "docs/zz_unique_docs/**")


def _config(config, language, **extra):
    lanes = {slug: {**values, "paths": list(PATHS)} for slug, values in config.lanes.items()}
    return replace(config, language=language, lanes=lanes, person_name=PERSON, **extra)


def _frame(snapshot, config, tmp_path, width=160):
    from foragent.watch import render_frame

    layout = {}
    frame = render_frame(snapshot, config, tmp_path, width=width, tick=0, layout=layout)
    return plain_frame(frame), layout, frame


def _label(frame, number):
    """Every drawn label of acorn #number: what stands between '#N' and the acorn's end."""
    return [match.strip() for match in re.findall(rf"#{number}(?!\d)([^▌\n]*)▌", frame)]


def _one_label(frame, number):
    labels = _label(frame, number)
    assert len(labels) == 1, f"acorn #{number} is drawn once as a sideways acorn:\n{frame}"
    return labels[0]


def _worker(phase="implementing", state="working", asked=None, parked=False):
    snapshot, config = single_worker(phase=phase, state=state)
    if asked is not None:
        snapshot = replace(snapshot, questions={101: asked})
    if parked:
        snapshot = replace(snapshot, park_facts={101: {"parked": True}})
    return snapshot, config


# --- Statement 1: number and one stage word, no person, PM or role name -----------------

@pytest.mark.parametrize("phase,word", [
    ("baseline", "구현"), ("implementing", "구현"), ("resolving", "구현"),
    ("checking", "확인"), ("ci", "CI"), ("merging", "차례"), ("merge-lock-wait", "차례"),
])
def test_korean_working_acorn_says_its_number_and_one_stage_word(tmp_path, phase, word):
    snapshot, config = _worker(phase=phase)
    frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
    label = _one_label(frame, 101)
    assert label == word, f"phase {phase}: the acorn reads '#101 {word}', not '#101 {label}'"


def test_korean_question_for_the_person_says_only_jilmun(tmp_path):
    # The person's turn as the question projection names it, then a needs-human acorn
    # without any projection: both read '#101 질문' and nothing more.
    for asked in ("person", None):
        snapshot, config = _worker(phase=None, state="waiting-human", asked=asked)
        frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
        label = _one_label(frame, 101)
        assert label == "질문", (
            f"a person's turn (projection {asked}) reads '#101 질문', with no name: '#101 {label}'")


@pytest.mark.parametrize("asked", ["orchestrator", "answered", "merge-owner"])
def test_korean_machine_turn_is_one_machine_word_without_a_role_name(tmp_path, asked):
    snapshot, config = _worker(phase=None, state="waiting-human", asked=asked)
    frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
    label = _one_label(frame, 101)
    assert label in MACHINE_KO, (
        f"{asked}'s turn is machine work: one word of {sorted(MACHINE_KO)}, never 질문 or a name; "
        f"got '#101 {label}'")


def test_korean_parked_acorn_says_meomchum(tmp_path):
    snapshot, config = _worker(parked=True)
    frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
    assert _one_label(frame, 101) == "멈춤"


# Every phase and state a worker acorn can be drawn in, with its exact word where the order
# names one (None: any one word of the vocabulary).
EVERY_KO = [
    ("resolving-conflict", "working", None), (None, "working", None),
    ("reviewing", "working", "리뷰"), ("merge-owner-wait", "working", "차례"),
    *[(phase, "working", None) for phase in ("overlap-wait", "test-slot-wait", "baseline-wait",
                                              "limit-wait", "executor-wait", "run-slot-wait",
                                              "an-unknown-phase")],
    ("implementing", "blocked", None), (None, "blocked", None),
]


def test_every_korean_acorn_label_is_one_word_of_the_stage_vocabulary(tmp_path):
    wrong = []
    for phase, state, word in EVERY_KO:
        snapshot, config = _worker(phase=phase, state=state)
        frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
        label = _one_label(frame, 101)
        if label not in VOCABULARY_KO or (word is not None and label != word):
            wrong.append(f"{phase}/{state}: '#101 {label}'" + (f" (expected '#101 {word}')" if word else ""))
    assert not wrong, (
        f"every acorn's label is one word of {sorted(VOCABULARY_KO)}; these are not:\n"
        + "\n".join(wrong))


EVERY_EN = [
    dict(phase=None, state="waiting-human", asked="person"),
    dict(phase=None, state="waiting-human", asked="orchestrator"),
    dict(phase=None, state="waiting-human", asked="answered"),
    dict(phase=None, state="waiting-human", asked="merge-owner"),
    dict(phase="implementing"), dict(phase="checking"), dict(phase="reviewing"), dict(phase="ci"),
    dict(phase="merging"), dict(phase="overlap-wait"), dict(phase=None, state="blocked"),
    dict(parked=True),
]


def test_english_acorn_label_is_one_word_without_a_name(tmp_path):
    labels, wrong = {}, []
    for case in EVERY_EN:
        snapshot, config = _worker(**case)
        frame, _, _ = _frame(snapshot, _config(config, "en"), tmp_path)
        label = labels[case.get("asked")] = _one_label(frame, 101)
        if not re.fullmatch(r"[A-Za-z]+", label) or label.lower() in {n.lower() for n in NAMES}:
            wrong.append(f"{case}: '#101 {label}'")
    assert not wrong, "'#101' and one English word with no name; these are not:\n" + "\n".join(wrong)
    # The word still tells a person's turn from machine work.
    machine = {labels[asked] for asked in ("orchestrator", "answered", "merge-owner")}
    assert labels["person"] not in machine, labels


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("asked", ["person", "orchestrator", "answered", "merge-owner"])
def test_no_acorn_names_the_person_pm_or_a_role_even_when_it_is_short(tmp_path, language, asked):
    """Every width keeps names off the acorn, including the ID-only acorn of a tight row."""
    snapshot, config = _worker(phase=None, state="waiting-human", asked=asked)
    config = _config(config, language)
    for width in (160, 60, 44, 40, 30):
        frame, _, _ = _frame(snapshot, config, tmp_path, width=width)
        drawn = [line for line in frame.splitlines() if re.search(r"#101(?!\d)", line) and "▌" in line]
        if width >= 40:
            assert drawn, f"width {width}: acorn #101 is drawn:\n{frame}"
        for line in drawn:
            acorn = line[line.index("#101"):]
            acorn = acorn[:acorn.index("▌")] if "▌" in acorn else acorn
            for name in NAMES:
                assert not re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", acorn), (
                    f"{language}/{asked} width {width}: no name {name!r} on the acorn {acorn!r}")
            assert ":" not in acorn, f"no 'word: name' acorn at width {width}: {acorn!r}"


# --- Statement 2: lane rows show no file paths ------------------------------------------

@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("width", [160, 80, 30])
def test_lane_rows_show_no_file_paths(tmp_path, language, width):
    snapshot, config = _worker()
    snapshot.status["lanes"]["work"]["queued"] = [{"number": n, "title": f"Task {n}"} for n in QUEUED]
    config = _config(config, language)
    frame, layout, raw = _frame(snapshot, config, tmp_path, width=width)
    assert ("squirrel", "work") in hit_positions(layout, raw, width), "the lane block is drawn"
    for path in PATHS:
        stem = path.split("/**")[0]
        assert stem not in frame, f"lane rows show no file path ({path!r}) at width {width}:\n{frame}"
    assert "/**" not in frame, f"no path glob anywhere on the lane rows:\n{frame}"


def test_lane_rows_keep_the_name_and_todays_merges_without_paths(tmp_path):
    snapshot, config = _worker()
    snapshot = replace(snapshot, today_done={"work": 3}, today=3)
    frame, _, _ = _frame(snapshot, _config(config, "ko"), tmp_path)
    name_rows = [line for line in frame.splitlines() if "오늘 3개 머지" in line]
    assert len(name_rows) == 1, f"the lane's name row still says today's merges once:\n{frame}"
    assert "zz_unique" not in name_rows[0] and "," not in name_rows[0].split("오늘")[0], (
        f"the name row carries no path list: {name_rows[0]!r}")


# --- Statement 3: next work as one count line --------------------------------------------

def _queued(language, count=len(QUEUED), extra_lane=False):
    snapshot, config = _worker()
    numbers = QUEUED[:count]
    snapshot.status["lanes"]["work"]["queued"] = [{"number": n, "title": f"Task {n}"} for n in numbers]
    config = _config(config, language)
    if extra_lane:
        from copy import deepcopy

        row = deepcopy(snapshot.status["lanes"]["work"])
        row.update(slug="other", state="queued", issue=301, title="Task 301", phase=None,
                   running=[], held=[], queued=[{"number": n, "title": f"Task {n}"} for n in (301, 302, 303)])
        snapshot.status["lanes"]["other"] = row
        config = replace(config, lanes={**config.lanes, "other": {**config.lanes["work"], "title": "other"}})
        snapshot = replace(snapshot, done={"work": [], "other": []})
    return snapshot, config, numbers


KO_COUNT = r"다음\s*{n}\s*개\s*▸\s*누르면\s*목록"


@pytest.mark.parametrize("count", [5, 1])
def test_korean_next_work_is_one_count_line_and_no_next_acorns(tmp_path, count):
    snapshot, config, numbers = _queued("ko", count)
    frame, layout, raw = _frame(snapshot, config, tmp_path)
    lines = [line for line in frame.splitlines() if re.search(KO_COUNT.format(n=count), line)]
    assert len(lines) == 1, f"one '다음 {count}개 ▸ 누르면 목록' line:\n{frame}"
    assert len(re.findall(r"다음\s*\d+\s*개", frame)) == 1, f"the count line appears once:\n{frame}"
    for n in numbers:
        assert not re.search(rf"#{n}(?!\d)", frame), f"next acorn #{n} is not listed:\n{frame}"
    hits = hit_positions(layout, raw, 160)
    assert not [n for n in numbers if ("acorn", n) in hits], "next acorns are not drawn as acorns"
    assert ("acorn", 101) in hits, "the working acorn stays"
    # A lane with nothing next draws no count line at all.
    empty, config = _worker()
    frame, _, _ = _frame(empty, _config(config, "ko"), tmp_path)
    assert not re.search(r"누르면\s*목록|다음\s*\d+\s*개", frame.split("╰")[-1]), frame


def test_each_lane_counts_its_own_next_work(tmp_path):
    snapshot, config, _ = _queued("ko", 5, extra_lane=True)
    frame, _, _ = _frame(snapshot, config, tmp_path)
    counts = sorted(int(n) for n in re.findall(r"다음\s*(\d+)\s*개\s*▸", frame))
    assert counts == [3, 5], f"each lane has one count line with its own count:\n{frame}"
    for n in (*QUEUED, 302, 303):
        assert not re.search(rf"#{n}(?!\d)", frame), f"#{n} is counted, not listed:\n{frame}"


def test_english_next_work_is_one_count_line(tmp_path):
    snapshot, config, numbers = _queued("en", 5)
    frame, _, _ = _frame(snapshot, config, tmp_path)
    lines = [line for line in frame.splitlines()
             if re.search(r"\bnext\b", line, re.IGNORECASE) and re.search(r"(?<![#\d])5(?!\d)", line)
             and "▸" in line]
    assert len(lines) == 1, f"one 'next 5 ▸ …' count line:\n{frame}"
    for n in numbers:
        assert not re.search(rf"#{n}(?!\d)", frame), f"next acorn #{n} is not listed:\n{frame}"


@pytest.mark.parametrize("glyphs", ["ascii", "auto"])
def test_text_fallback_counts_next_work_too(tmp_path, glyphs):
    """Narrow or ASCII terminals list no next acorns either; one count line stands for them."""
    snapshot, config, numbers = _queued("ko", 5)
    config = replace(config, view_glyphs=glyphs)
    frame, _, _ = _frame(snapshot, config, tmp_path, width=32 if glyphs == "auto" else 120)
    assert len(re.findall(r"다음\s*5\s*개", frame)) == 1, f"one count line in the text fallback:\n{frame}"
    for n in numbers:
        assert not re.search(rf"#{n}(?!\d)", frame), f"#{n} is counted, not listed:\n{frame}"


# --- Through the dashboard the person opens (`fa dashboard --once`, now-snapshot) --------

@pytest.mark.parametrize("language", ["ko", "en"])
def test_dashboard_once_draws_short_acorns_no_paths_and_a_next_count(
        git_history, tmp_path, monkeypatch, capsys, language):
    from dashboard_once_helpers import _once, _populated

    case, main = _populated(git_history, tmp_path, monkeypatch, language)
    code, rows = _once(main, case, capsys, 140)
    frame = "\n".join(rows)
    assert code == 0, frame
    # #2 implements in the view lane; #5 waits for the person; #3 and #4 are docs' next work.
    label = _one_label(frame, 2)
    question = _one_label(frame, 5)
    if language == "ko":
        assert label == "구현", f"'#2 구현', got '#2 {label}':\n{frame}"
        assert question == "질문", f"'#5 질문' with no name, got '#5 {question}':\n{frame}"
        assert len(re.findall(KO_COUNT.format(n=2), frame)) == 1, f"docs: '다음 2개 ▸ 누르면 목록':\n{frame}"
    else:
        for value in (label, question):
            assert re.fullmatch(r"[A-Za-z]+", value), f"one English word per acorn, got {value!r}:\n{frame}"
            assert value.lower() not in {name.lower() for name in NAMES}
        assert len([row for row in rows if re.search(r"\bnext\b", row, re.IGNORECASE)
                    and re.search(r"(?<![#\d])2(?!\d)", row) and "▸" in row]) == 1, frame
    for n in (3, 4):
        assert not re.search(rf"#{n}(?!\d)", frame), f"#{n} is counted, not listed:\n{frame}"
    for path in ("src/view/**", "docs/**", "src/view", "docs/"):
        assert path not in frame, f"lane rows show no file path {path!r}:\n{frame}"
    for name in ("진님", "오케스트레이터"):
        assert not any(name in line and "▌" in line for line in rows), f"no {name} on an acorn:\n{frame}"

"""Review regressions for the lane scene (#1062): question state, explanation rows, acorn flow."""

import pytest

from foragent import watch
from foragent.config import Config
from foragent.watch_data import Snapshot
from foragent.watch_sections import lane_blocks, lane_bubble
from helpers.cells import cells
from test_watch import NOW


@pytest.fixture
def utf8(monkeypatch):
    """A UTF-8 colour terminal, so the scene may draw pixels (glyph_mode reads these)."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")


def _text(line):
    return "".join(value for value, _ in line)


def _blocks(snapshot, config, width):
    """The lane blocks as drawn (rows of text), their hits and their keyboard marks."""
    hits, marks = [], []
    out = lane_blocks.render(snapshot, config, ".", width=width, now=NOW, hits=hits, marks=marks)
    return [_text(line) for line in out], hits, marks


def _acorn_hits(hits):
    """(row, x0, x1, number) of every acorn the frame can click, in drawing order."""
    return [(y0, x0, x1, target[1]) for x0, y0, x1, _, target in hits if target[0] == "acorn"]


def _running(numbers, **extra):
    """One working lane whose running acorns are ``numbers``, each with a live run."""
    row = {"slug": "work", "lane_display": "work", "state": "working", "issue": numbers[0], "phase": "implementing",
           "running": [{"number": n, "title": "T"} for n in numbers], "queued": [], "held": []}
    snapshot = _snapshot(row, worker_states={n: "working" for n in numbers}, **extra)
    snapshot.status["active_runs"] = [{"number": n, "lane": "work", "status": "running", "phase": "implementing",
                                       "started_at": NOW.isoformat()} for n in numbers]
    return snapshot


def _snapshot(row, **extra):
    status = {"lanes": {"work": row}, "active_runs": [], "issues": {}}
    return Snapshot(status=status, done={}, order=None, completed=0, total=0, eta=None, events=[],
                    captured_at=NOW, **extra)


def _classes(snapshot, row):
    from foragent.watch_scene import lane_items

    current, upcoming = lane_items(snapshot, row)
    return {item["number"]: item["class"] for item in current + upcoming}


@pytest.mark.parametrize("listing", ["running", "queued"])
def test_an_items_own_waiting_human_state_makes_a_question(listing):
    row = {"slug": "work", "state": "working", "issue": 1, "phase": "implementing",
           "running": [{"number": 1, "title": "One"}], "queued": [], "held": []}
    row[listing] = row[listing] + [{"number": 7, "title": "Ask", "state": "waiting-human"}]
    snapshot = _snapshot(row, worker_states={1: "working"})
    classes = _classes(snapshot, row)
    assert classes[7] == "question" and classes[1] == "working"


def test_a_held_question_for_a_running_issue_is_not_lost_to_deduplication():
    row = {"slug": "work", "state": "working", "issue": 1, "phase": "implementing",
           "running": [{"number": 1, "title": "One"}, {"number": 2, "title": "Two"}], "queued": [],
           "held": [{"number": 2, "title": "Two", "state": "waiting-human"}]}
    snapshot = _snapshot(row, worker_states={1: "working", 2: "working"})
    assert _classes(snapshot, row) == {1: "working", 2: "question"}


def test_long_lane_metadata_keeps_the_step_time_and_warning_rows(utf8):
    from datetime import timedelta

    from foragent.watch_sections.lane_bubble import BODY_ROWS

    snapshot = _running([5], log_ages={5: 1800})
    snapshot.status["lanes"]["work"]["running"] = [{"number": 5, "title": "Title"}]
    snapshot.status["active_runs"][0]["phase_started_at"] = (NOW - timedelta(minutes=12)).isoformat()
    config = Config(language="en", lanes={"work": {"paths": [f"area{n}/**" for n in range(40)]}})
    # A 34-cell box without the sprite holds 30 cells of explanation.
    out = lane_bubble.render(snapshot, config, ".", width=34, now=NOW, state={"explanation_target": ("acorn", 5)})
    box = [_text(line) for line in out[:BODY_ROWS + 2]]
    assert box[0].startswith("╭") and box[-1].startswith("╰")
    rows = [text[2:-2].rstrip() for text in box[1:-1]]
    rows = rows[:max(index for index, text in enumerate(rows) if text) + 1]
    assert len(rows) <= BODY_ROWS
    assert rows[0] == "#5 Title" and rows[-3:] == [
        "writing code", "12m in this step", "? quiet for 30 min"], rows
    assert rows[1].startswith("lane") and rows[2].endswith("…") and len(rows) == BODY_ROWS


@pytest.mark.parametrize("ascii_glyphs", [False, True])
def test_oversized_first_acorns_still_leave_room_for_the_exact_omitted_count(utf8, ascii_glyphs):
    # Ten working acorns, each with a long quiet warning, in a 40-cell pixel frame (26 cells
    # of content beside the squirrel): two rows, then the exact count of the rest.
    snapshot = _running(list(range(100, 110)), log_ages={n: 1800 for n in range(100, 110)})
    texts, hits, _ = _blocks(snapshot, Config(language="en", lanes={"work": {}}), 40)
    placed = _acorn_hits(hits)
    assert all(cells(text) <= 40 for text in texts), texts
    last = texts[max(y for y, *_ in placed)].rstrip()
    assert last.endswith(f"…{10 - len(placed)}") and len(placed) >= 2, texts
    if ascii_glyphs:
        # ASCII glyphs are a text fallback: every acorn on its own row, none left to count.
        config = Config(language="en", lanes={"work": {}}, view_glyphs="ascii")
        texts, hits, _ = _blocks(snapshot, config, 40)
        placed = _acorn_hits(hits)
        assert all(cells(text) <= 40 for text in texts), texts
        assert sorted(number for *_, number in placed) == list(range(100, 110))
        assert len({y for y, *_ in placed}) == 10 and not any("..." + str(n) in "".join(texts)
                                                               for n in range(1, 10)), texts


@pytest.mark.parametrize("asked", ["listing", "needs-human"])
def test_a_lane_of_only_queued_questions_is_a_question_lane_without_a_turn_sentence(asked):
    from foragent.watch_scene import category, lane_items, representative_wait

    item = {"number": 9, "title": "Ask"}
    if asked == "listing":
        item["state"] = "waiting-human"
    row = {"slug": "work", "state": "queued", "issue": 9, "running": [], "held": [], "queued": [item]}
    snapshot = _snapshot(row)
    if asked == "needs-human":
        snapshot.status["issues"] = {"needs-human": [{"number": 9}]}
    current, upcoming = lane_items(snapshot, row)
    assert [entry["class"] for entry in upcoming] == ["question"]
    assert category(current, upcoming) == "question"
    assert representative_wait(row, current, upcoming) is None


def test_the_turn_sentence_belongs_to_a_queued_next_acorn_not_a_parked_one():
    from foragent.watch_scene import lane_items, representative_wait

    row = {"slug": "work", "state": "queued", "issue": 3, "running": [], "held": [],
           "queued": [{"number": 3, "title": "Parked"}, {"number": 4, "title": "Next"}]}
    snapshot = _snapshot(row, park_facts={3: {"parked": True, "at": NOW.isoformat()}})
    current, upcoming = lane_items(snapshot, row)
    assert representative_wait(row, current, upcoming) == 4


@pytest.mark.parametrize("ascii_glyphs", [False, True])
def test_marked_fit_shows_every_cut_and_leaves_fitting_lines_alone(ascii_glyphs):
    from foragent.watch_scene import fit_marked

    mark = "..." if ascii_glyphs else "…"
    line = [("LongLaneName" * 5, "bold"), (" ✓3", "green")]
    cut = "".join(value for value, _ in fit_marked(line, 40, mark))
    assert cells(cut) <= 40 and cut.endswith(mark) and cut.startswith("LongLaneName")
    assert fit_marked([("short", "")], 40, mark) == [("short", "")]


def test_overlong_lane_and_idle_names_and_evidence_are_cut_with_the_mark(utf8):
    # A working lane (its name row) and a resting one (the shared idle block), both named
    # far wider than a 40-cell text frame, with merges today.
    long = "LongLaneName" * 5
    busy, idle = long + "A", long + "B"
    working = {"slug": busy, "lane_display": busy, "state": "working", "issue": 1, "phase": "implementing",
               "running": [{"number": 1, "title": "T"}], "queued": [], "held": []}
    resting = {"slug": idle, "lane_display": idle, "state": "idle", "running": [], "queued": [], "held": []}
    snapshot = _snapshot(working, today_done={busy: 2, idle: 2}, worker_states={1: "working"})
    snapshot.status["lanes"] = {busy: working, idle: resting}
    config = Config(language="en", lanes={busy: {}, idle: {}}, animation=False)
    texts, hits, _ = _blocks(snapshot, config, 40)
    text = next(text for text in texts if text.startswith("LongLaneName"))
    assert text.rstrip().endswith("…") and cells(text) <= 40 and long[:20] in text
    names = texts[-1].rstrip()
    assert names.endswith("…") and cells(names) <= 40 and names.startswith("LongLaneName")
    idle_hits = [hit for hit in hits if hit[1] == len(texts) - 1]
    assert idle_hits and idle_hits[0][4] == ("squirrel", idle)


def test_a_bar_label_wider_than_the_row_is_cut_with_the_mark(utf8):
    row = {"slug": "work", "running": [], "queued": [], "held": []}
    snapshot = _snapshot(row, today=8)
    snapshot.status["issues"] = {"running": [{"number": 1}, {"number": 2}]}
    # The bar follows the bubble, as its last row.
    out = lane_bubble.render(snapshot, Config(language="en", lanes={"work": {}}), ".", width=12, now=NOW)
    text = _text(out[-1])
    assert text.endswith("…") and cells(text) <= 12 and text.startswith("80%")


@pytest.mark.parametrize("fallback", ["animation-off", "ascii", "non-utf8", "narrow"])
def test_every_text_fallback_gives_each_acorn_its_own_selectable_row(monkeypatch, fallback):
    from types import SimpleNamespace

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "C" if fallback == "non-utf8" else "en_US.UTF-8")
    config = SimpleNamespace(language="en", lanes={"work": {}}, animation=fallback != "animation-off",
                             view_glyphs="ascii" if fallback == "ascii" else "auto")
    row = {"slug": "work", "lane_display": "work", "state": "working", "issue": 101, "phase": "implementing",
           "running": [{"number": n, "title": "T"} for n in (101, 102, 103)],
           "queued": [{"number": n, "title": "N"} for n in (201, 202, 203)], "held": []}
    snapshot = _snapshot(row, worker_states={101: "working", 102: "working", 103: "working"})
    width = 30 if fallback == "narrow" else 160
    texts, hits, marks = _blocks(snapshot, config, width)
    # A text fallback draws no squirrel art: the lane name's one row is the squirrel's hit.
    squirrels = [(y1 - y0, target) for _, y0, _, y1, target in hits if target[0] == "squirrel"]
    assert squirrels == [(1, ("squirrel", "work"))], "every case is a text fallback"
    numbers = (101, 102, 103, 201, 202, 203)
    assert sorted(number for _, number, _ in marks) == sorted(numbers), "each acorn is reachable by keyboard"
    for number in numbers:
        rows = [text for text in texts if f"#{number}" in text]
        assert len(rows) == 1 and sum(f"#{n}" in rows[0] for n in numbers) == 1, (number, rows)
    assert {target for *_, target in hits if target[0] == "acorn"} == {("acorn", n) for n in numbers}


@pytest.mark.parametrize("ascii_glyphs", [True, False])
@pytest.mark.parametrize("width", [8, 10, 12])
def test_narrow_acorns_keep_their_complete_id_before_the_step_word_or_the_prefix(utf8, ascii_glyphs, width):
    # A working acorn with a step word (no prefix) and a queued one under the "next" prefix.
    snapshot = _running([1062])
    snapshot.status["lanes"]["work"]["queued"] = [{"number": 1063, "title": "N"}]
    config = Config(language="en", lanes={"work": {}}, view_glyphs="ascii" if ascii_glyphs else "auto")
    texts, hits, _ = _blocks(snapshot, config, width)
    placed = _acorn_hits(hits)
    assert all(cells(text) <= width for text in texts), texts
    for number in (1062, 1063):
        rows = [index for index, text in enumerate(texts) if f"#{number}" in text]
        assert len(rows) == 1, (number, texts)
        assert [entry[3] for entry in placed if entry[0] == rows[0]] == [number]
    assert not any(text.rstrip().endswith(("#10", "#106")) for text in texts), texts


def test_an_id_wider_than_the_row_is_cut_with_the_mark_not_silently(utf8):
    texts, hits, _ = _blocks(_running([1062]), Config(language="en", lanes={"work": {}}, view_glyphs="ascii"), 5)
    placed = _acorn_hits(hits)
    text = texts[placed[0][0]]
    assert text.endswith("...") and cells(text) <= 5 and [entry[3] for entry in placed] == [1062]


def test_an_id_that_fits_beside_the_count_is_drawn_not_counted(utf8):
    # A 40-cell pixel frame leaves 26 cells beside the squirrel: one acorn a row, and the
    # second (last) row still draws its acorn beside the count of the one left out.
    texts, hits, _ = _blocks(_running([1062, 1063, 1064]), Config(language="en", lanes={"work": {}}), 40)
    placed = _acorn_hits(hits)
    assert [entry[3] for entry in placed] == [1062, 1063]
    second = texts[placed[1][0]]
    assert placed[1][0] == placed[0][0] + 1 and "#1063" in second
    assert second.rstrip().endswith("…1") and cells(second) <= 40


@pytest.mark.parametrize("listing", ["running", "held", "queued", "facts-only"])
@pytest.mark.parametrize("pixels", [False, True])
def test_authored_waits_keep_their_acorn_and_all_explanation_rows_outside_flow_overflow(utf8, listing, pixels):

    row = {"slug": "work", "lane_display": "work", "state": "idle", "issue": None,
           "running": [], "held": [], "queued": [{"number": n, "title": "Next"} for n in range(40)]}
    if listing != "facts-only":
        row[listing].append({"number": 999, "title": "Authored wait", "state": "blocked"})
    snapshot = _snapshot(row, waiting_facts={999: {"lane": "work", "title": "Authored wait", "authored": {
        "text": "Canonical wait", "reason": "Choice  pending", "next_condition": "Record choice",
        "responsible_role": "orchestrator"}}})
    # Pixels draw 60 cells of content beside the squirrel; the text case uses ASCII glyphs.
    config = Config(language="en", lanes={"work": {}}, view_glyphs="auto" if pixels else "ascii")
    left = lane_blocks.CONTENT_FROM if pixels else 0
    texts, hits, marks = _blocks(snapshot, config, 60 + left)
    lines = [text[left:] for text in texts]
    # The block starts under its rule; the orchestrator line above it is not the block's.
    text = "\n".join(lines[next(index for index, line in enumerate(texts) if line.startswith(("-", "━"))):])
    own = [hit for hit in hits if hit[-1] == ("acorn", 999)]
    assert len(own) == 1 and [number for _, number, _ in marks].count(999) == 1
    y = own[0][1]
    assert "#999" in lines[y]
    assert [line.strip() for line in lines[y + 1:y + 4]] == [
        "Waiting: Choice  pending", "Next: Record choice", "Owner: orchestrator"]
    for phrase in ("Choice  pending", "Record choice", "orchestrator"):
        assert text.count(phrase) == 1
    assert all(cells(line) <= 60 for line in lines)


def test_authored_wait_uses_its_recorded_lane_when_a_listing_still_names_another_lane():
    from foragent.watch_scene import lane_items

    old = {"slug": "old", "state": "idle", "issue": None, "running": [], "queued": [], "held": []}
    new = {**old, "slug": "new", "queued": [{"number": 999, "title": "Moved"}]}
    snapshot = _snapshot(old, waiting_facts={999: {"lane": "old", "title": "Recorded title",
                                                   "authored": {"text": "Still waiting"}}})
    old_items = sum(lane_items(snapshot, old), [])
    assert [(item["number"], item["title"]) for item in old_items] == [(999, "Recorded title")]
    assert sum(lane_items(snapshot, new), []) == []


def _running_row(*running, **extra):
    return {"slug": "work", "state": "working", "issue": 1, "phase": "implementing", "queued": [], "held": [],
            "running": [{"number": 1, "title": "One"}, *running], **extra}


def _run(number):
    return {"number": number, "lane": "work", "status": "running", "phase": "implementing", "started_at": None}


def _acorn_and_click(snapshot, tmp_path, number):
    """The word the drawn acorn says beside its number, and the click's explanation."""
    import re

    from foragent.watch_sections.lane_bubble import explain

    # ASCII acorns print as [#N word] in a wide text frame, one acorn per row.
    config = Config(language="en", lanes={"work": {}}, view_glyphs="ascii")
    texts, _, _ = _blocks(snapshot, config, 160)
    words = [found.group(1) for text in texts for found in re.finditer(rf"\[#{number}(?: ([^\]]*))?\]", text)]
    assert len(words) == 1, texts
    rows = explain(("acorn", number), snapshot, Config(language="en"), tmp_path, now=NOW, width=70, state={})
    return words[0] or "", "\n".join("".join(value for value, _ in line) for line in rows)


@pytest.mark.parametrize("with_run", [True, False], ids=["active-run", "listed-only"])
@pytest.mark.parametrize("states", [("waiting-human", "working"), ("working", "waiting-human"),
                                    ("waiting-human", None), (None, "waiting-human"),
                                    ("blocked", "waiting-human"), ("waiting-human", "blocked")])
def test_duplicate_running_entries_keep_the_question_in_any_order(states, with_run, tmp_path):
    row = _running_row(*[{"number": 7, "title": "Ask", **({"state": state} if state else {})} for state in states])
    snapshot = _snapshot(row)
    snapshot.status["active_runs"] = [_run(7)] if with_run else []
    assert _classes(snapshot, row)[7] == "question"
    word, click = _acorn_and_click(snapshot, tmp_path, 7)
    expected = watch.text("lane_bubble_question_click", "en", who=watch.text("question_person", "en"))
    assert word == "question" and expected in click


@pytest.mark.parametrize("with_run", [True, False], ids=["active-run", "listed-only"])
@pytest.mark.parametrize("where", ["held", "running-state", "running-duplicate", "worker-state", "queued-too"])
def test_a_running_issue_recorded_as_blocked_anywhere_is_a_wait_acorn(where, with_run, tmp_path):
    block = {"number": 7, "title": "Stuck", "state": "blocked"}
    plain = {"number": 7, "title": "Stuck"}
    row = _running_row({"held": plain, "worker-state": plain, "queued-too": plain}.get(where, block))
    if where == "held":
        row["held"] = [block]
    elif where == "queued-too":
        row["queued"] = [block]
    elif where == "running-duplicate":
        row["running"] = [row["running"][0], plain, block, plain]
    snapshot = _snapshot(row, **({"worker_states": {7: "blocked"}} if where == "worker-state" else {}))
    snapshot.status["active_runs"] = [_run(7)] if with_run else []
    assert _classes(snapshot, row)[7] == "wait"
    word, click = _acorn_and_click(snapshot, tmp_path, 7)
    assert word == "wait" and watch.text("blocked", "en") in click and "writing code" not in click


def test_a_block_or_question_never_gives_way_to_a_weaker_later_record():
    row = _running_row({"number": 7, "state": "waiting-human"}, {"number": 7, "state": "blocked"},
                       {"number": 8, "state": "blocked"}, {"number": 8, "state": "working"})
    row["held"] = [{"number": 7, "state": "working"}, {"number": 8, "state": "working"}]
    snapshot = _snapshot(row, worker_states={7: "working", 8: "working"})
    classes = _classes(snapshot, row)
    assert classes[7] == "question" and classes[8] == "wait" and classes[1] == "working"


def test_a_records_block_does_not_spread_to_the_other_running_issues():
    row = _running_row({"number": 7, "state": "blocked"}, {"number": 8})
    assert _classes(_snapshot(row), row) == {1: "working", 7: "wait", 8: "working"}


@pytest.mark.parametrize("listing", ["running", "held"])
def test_a_later_record_of_the_wait_phase_reaches_the_acorn(listing, tmp_path):
    row = _running_row({"number": 7, "title": "Slot"}, {"number": 7, "title": "Slot", "phase": "test-slot-wait"})
    if listing == "held":
        row["running"].pop()
        row["held"] = [{"number": 7, "title": "Slot", "state": "working", "phase": "test-slot-wait"}]
    snapshot = _snapshot(row)
    word, click = _acorn_and_click(snapshot, tmp_path, 7)
    assert word == "wait: test slot" and watch.text("test-slot-wait", "en") in click


def test_a_live_runs_phase_beats_a_listing_of_the_same_issue():
    from foragent.watch_scene import lane_items

    row = _running_row({"number": 7, "phase": "test-slot-wait"})
    snapshot = _snapshot(row)
    snapshot.status["active_runs"] = [_run(7)]
    assert next(item for item in lane_items(snapshot, row)[0] if item["number"] == 7)["phase"] == "implementing"


def test_a_queued_issue_keeps_its_recorded_block_and_says_why_the_lane_waits(tmp_path):
    from foragent.watch_scene import category, lane_items, representative_wait

    row = {"slug": "work", "state": "queued", "issue": 7, "running": [], "held": [],
           "queued": [{"number": 7, "title": "Stuck", "state": "blocked"}, {"number": 8, "title": "Next"}]}
    snapshot = _snapshot(row)
    current, upcoming = lane_items(snapshot, row)
    assert [item["class"] for item in upcoming] == ["wait", "next"] and current == []
    assert category(current, upcoming) == "waiting" and representative_wait(row, current, upcoming) == 7
    word, click = _acorn_and_click(snapshot, tmp_path, 7)
    assert word == "wait" and watch.text("blocked", "en") in click


@pytest.mark.parametrize("listing, recorded, expected", [
    ("held", "waiting-human", "question"), ("queued", "waiting-human", "question"), ("queued", "blocked", "wait")])
def test_the_workers_own_recorded_state_reaches_a_held_or_queued_acorn(listing, recorded, expected):
    row = {"slug": "work", "state": "working", "issue": 1, "running": [{"number": 1}], "queued": [],
           "held": [], listing: [{"number": 7, "state": "blocked" if listing == "held" else "queued"}]}
    assert _classes(_snapshot(row, worker_states={7: recorded}), row)[7] == expected

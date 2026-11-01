"""Review regressions for the lane scene (#1062): question state, explanation rows, acorn flow."""

import pytest

from foragent import watch
from foragent.watch_data import Snapshot
from test_watch import NOW


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


def test_long_lane_metadata_keeps_the_step_time_and_warning_rows():
    from foragent.watch_sections.lane_bubble import BODY_ROWS, _explanation_rows

    found = [[("#5 Title", "bold")], [("lane " + ", ".join(f"area{n}/**" for n in range(40)), "")],
             [("writing code", "")], [("12m", "")], [("quiet for 9m", "yellow")]]
    rows = ["".join(value for value, _ in row) for row in _explanation_rows(found, 30, "…")]
    assert len(rows) <= BODY_ROWS
    assert rows[0] == "#5 Title" and rows[-3:] == ["writing code", "12m", "quiet for 9m"]
    assert rows[1].startswith("lane") and rows[2].endswith("…") and len(rows) == BODY_ROWS


@pytest.mark.parametrize("ascii_glyphs", [False, True])
def test_oversized_first_acorns_still_leave_room_for_the_exact_omitted_count(ascii_glyphs):
    from foragent.watch_sections.lane_blocks import _flow
    from foragent.watch_art import sideways_acorn

    mark = "..." if ascii_glyphs else "…"
    units = [(sideways_acorn(100 + n, "write", "working", ascii=ascii_glyphs),
              sideways_acorn(100 + n, "", "working", ascii=ascii_glyphs),
              "quiet for 30 minutes, nothing written to the log", 100 + n) for n in range(10)]
    lines, placed = _flow(units, 40, 2, [], mark)
    texts = ["".join(value for value, _ in line) for line in lines]
    assert all(watch._cells(text) <= 40 for text in texts), texts
    assert texts[-1].endswith(f"{mark}{10 - len(placed)}") and len(placed) >= 2


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
    assert watch._cells(cut) <= 40 and cut.endswith(mark) and cut.startswith("LongLaneName")
    assert fit_marked([("short", "")], 40, mark) == [("short", "")]


def test_overlong_lane_and_idle_names_and_evidence_are_cut_with_the_mark():
    from types import SimpleNamespace

    from foragent.watch_sections.lane_blocks import _idle_block, _name_row

    long = "LongLaneName" * 5
    config = SimpleNamespace(language="en", lanes={long: {}})
    row = {"slug": long, "lane_display": long}
    snapshot = _snapshot(row, today_done={long: 2})
    line, _ = _name_row(snapshot, config, row, 40, "…")
    text = "".join(value for value, _ in line)
    assert text.endswith("…") and watch._cells(text) <= 40 and text.startswith("LongLaneName")
    lines, hits = _idle_block(snapshot, config, [row], 40, (False, False, False))
    names = "".join(value for value, _ in lines[-1])
    assert names.endswith("…") and watch._cells(names) <= 40
    assert hits and hits[0][4] == ("squirrel", long)


def test_a_bar_label_wider_than_the_row_is_cut_with_the_mark():
    from foragent.watch_sections.lane_bubble import _bar

    row = {"slug": "work", "running": [], "queued": [], "held": []}
    snapshot = _snapshot(row, today=8)
    snapshot.status["issues"] = {"running": [{"number": 1}, {"number": 2}]}
    text = "".join(value for value, _ in _bar(snapshot, "en", 12, "…")[0])
    assert text.endswith("…") and watch._cells(text) <= 12 and text.startswith("80%")


@pytest.mark.parametrize("fallback", ["animation-off", "ascii", "non-utf8", "narrow"])
def test_every_text_fallback_gives_each_acorn_its_own_selectable_row(monkeypatch, fallback):
    from types import SimpleNamespace

    from foragent.watch_scene import lane_items
    from foragent.watch_sections.lane_blocks import _block, _modes

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
    current, upcoming = lane_items(snapshot, row)
    width = 30 if fallback == "narrow" else 160
    modes = _modes(config, width)
    assert not modes[0], "every case is a text fallback"
    lines, hits, marks, _ = _block(snapshot, config, row, current, upcoming, width, 0, NOW, modes)
    numbers = (101, 102, 103, 201, 202, 203)
    assert sorted(number for _, number in marks) == sorted(numbers), "each acorn is reachable by keyboard"
    for number in numbers:
        rows = [text for text in ("".join(value for value, _ in line) for line in lines) if f"#{number}" in text]
        assert len(rows) == 1 and sum(f"#{n}" in rows[0] for n in numbers) == 1, (number, rows)
    assert {target for *_, target in hits} == {("acorn", n) for n in numbers}


@pytest.mark.parametrize("ascii_glyphs", [True, False])
@pytest.mark.parametrize("width", [8, 10, 12])
def test_narrow_acorns_keep_their_complete_id_before_the_step_word_or_the_prefix(ascii_glyphs, width):
    from foragent.watch_art import sideways_acorn
    from foragent.watch_sections.lane_blocks import _flow

    mark = "..." if ascii_glyphs else "…"
    units = [(sideways_acorn(n, word, style, ascii=ascii_glyphs), sideways_acorn(n, "", style, ascii=ascii_glyphs),
              "", n) for n, word, style in ((1062, "write", "working"), (1063, "", "next"))]
    for prefix in ([], [("next  ", "dim")]):
        lines, placed = _flow(units, width, 2, prefix, mark, 1)
        texts = ["".join(value for value, _ in line) for line in lines]
        assert all(watch._cells(text) <= width for text in texts), texts
        for number in (1062, 1063):
            rows = [index for index, text in enumerate(texts) if f"#{number}" in text]
            assert len(rows) == 1, (number, texts)
            assert [entry[3] for entry in placed if entry[0] == rows[0]] == [number]
        assert not any(text.rstrip().endswith(("#10", "#106")) for text in texts), texts


def test_an_id_wider_than_the_row_is_cut_with_the_mark_not_silently():
    from foragent.watch_art import sideways_acorn
    from foragent.watch_sections.lane_blocks import _flow

    units = [(sideways_acorn(1062, "write", "working", ascii=True), sideways_acorn(1062, "", "working", ascii=True),
              "", 1062)]
    lines, placed = _flow(units, 5, 1, [], "...", 1)
    text = "".join(value for value, _ in lines[0])
    assert text.endswith("...") and watch._cells(text) <= 5 and placed[0][3] == 1062


def test_an_id_that_fits_beside_the_count_is_drawn_not_counted():
    from foragent.watch_art import sideways_acorn
    from foragent.watch_sections.lane_blocks import _flow

    units = [(sideways_acorn(n, "write", "working", ascii=True), sideways_acorn(n, "", "working", ascii=True),
              "", n) for n in (1062, 1063, 1064)]
    lines, placed = _flow(units, 14, 2, [], "...", 1)
    texts = ["".join(value for value, _ in line) for line in lines]
    assert [entry[3] for entry in placed] == [1062, 1063]
    assert "[#1063]" in texts[1] and texts[1].endswith("...1") and watch._cells(texts[1]) <= 14


@pytest.mark.parametrize("listing", ["running", "held", "queued", "facts-only"])
@pytest.mark.parametrize("pixels", [False, True])
def test_authored_waits_keep_their_acorn_and_all_explanation_rows_outside_flow_overflow(listing, pixels):
    from foragent.config import Config
    from foragent.watch_scene import lane_items
    from foragent.watch_sections.lane_blocks import _block

    row = {"slug": "work", "lane_display": "work", "state": "idle", "issue": None,
           "running": [], "held": [], "queued": [{"number": n, "title": "Next"} for n in range(40)]}
    if listing != "facts-only":
        row[listing].append({"number": 999, "title": "Authored wait", "state": "blocked"})
    snapshot = _snapshot(row, waiting_facts={999: {"lane": "work", "title": "Authored wait", "authored": {
        "text": "Canonical wait", "reason": "Choice  pending", "next_condition": "Record choice",
        "responsible_role": "orchestrator"}}})
    config = Config(language="en", lanes={"work": {}})
    current, upcoming = lane_items(snapshot, row)
    lines, hits, marks, _ = _block(snapshot, config, row, current, upcoming, 60, 0, NOW,
                                  (pixels, not pixels, False))
    text = "\n".join("".join(value for value, _ in line) for line in lines)
    own = [hit for hit in hits if hit[-1] == ("acorn", 999)]
    assert len(own) == 1 and [number for _, number in marks].count(999) == 1
    y = own[0][1]
    assert "#999" in "".join(value for value, _ in lines[y])
    assert ["".join(value for value, _ in line).strip() for line in lines[y + 1:y + 4]] == [
        "Waiting: Choice  pending", "Next: Record choice", "Owner: orchestrator"]
    for phrase in ("Choice  pending", "Record choice", "orchestrator"):
        assert text.count(phrase) == 1
    assert all(sum(watch._cells(value) for value, _ in line) <= 60 for line in lines)


def test_authored_wait_uses_its_recorded_lane_when_a_listing_still_names_another_lane():
    from foragent.watch_scene import lane_items

    old = {"slug": "old", "state": "idle", "issue": None, "running": [], "queued": [], "held": []}
    new = {**old, "slug": "new", "queued": [{"number": 999, "title": "Moved"}]}
    snapshot = _snapshot(old, waiting_facts={999: {"lane": "old", "title": "Recorded title",
                                                   "authored": {"text": "Still waiting"}}})
    old_items = sum(lane_items(snapshot, old), [])
    assert [(item["number"], item["title"]) for item in old_items] == [(999, "Recorded title")]
    assert sum(lane_items(snapshot, new), []) == []

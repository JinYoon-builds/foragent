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

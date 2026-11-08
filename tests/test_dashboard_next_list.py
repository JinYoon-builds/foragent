"""short-acorn: a lane's next work is one count line, and a click on it lists the work in the
bubble. The list, the acorn's stage word and the review round in the click bubble are read
from the frame the public renderer draws, then clicked through `watch.handle_mouse`."""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_acorn_interact import hit_positions  # noqa: E402
from test_squirrel_running import plain_frame, single_worker, utf8_terminal  # noqa: E402,F401

QUEUED = (103, 104, 105)


def _frame(snapshot, config, tmp_path, state=None, width=160):
    from foragent.watch import render_frame

    layout = {}
    frame = render_frame(snapshot, config, tmp_path, width=width, tick=0, layout=layout, state=state)
    return plain_frame(frame), layout, frame


def _queued(language="ko"):
    snapshot, config = single_worker(phase="implementing")
    snapshot.status["lanes"]["work"]["queued"] = [{"number": n, "title": f"Task {n}"} for n in QUEUED]
    return snapshot, replace(config, language=language)


def _click(target, snapshot, config, tmp_path, state, width=160):
    from foragent import watch
    from foragent.state import StateStore

    _, layout, raw = _frame(snapshot, config, tmp_path, state=state, width=width)
    x, y = hit_positions(layout, raw, width)[target]
    watch.handle_mouse("left", x, y, layout, StateStore(tmp_path / "state"), config, state=state)


def test_a_click_on_the_next_count_lists_the_lanes_next_work_in_the_bubble(tmp_path):
    from foragent.watch_sections.lane_blocks import next_target

    snapshot, config = _queued()
    state = {}
    frame, layout, raw = _frame(snapshot, config, tmp_path, state=state)
    assert "다음 3개 ▸ 누르면 목록" in frame and "#103" not in frame
    _click(next_target("work"), snapshot, config, tmp_path, state)
    assert state["explanation_target"] == next_target("work")
    opened, _, _ = _frame(snapshot, config, tmp_path, state=state)
    bubble = [line for line in opened.splitlines() if "│" in line]
    for number in QUEUED:
        rows = [line for line in bubble if f"#{number} Task {number}" in line]
        assert len(rows) == 1, f"#{number} is listed once inside the bubble:\n{opened}"
    assert any("다음 3개" in line for line in bubble), opened
    # The same line closes the list again.
    _click(next_target("work"), snapshot, config, tmp_path, state)
    assert "explanation_target" not in state
    closed, _, _ = _frame(snapshot, config, tmp_path, state=state)
    assert "#103 Task 103" not in closed


def test_a_list_of_a_lane_without_next_work_leaves_the_default_lines(tmp_path):
    from foragent.watch_sections.lane_blocks import next_target

    snapshot, config = single_worker(phase="implementing")
    config = replace(config, language="ko")
    default, _, _ = _frame(snapshot, config, tmp_path, state={})
    listed, _, _ = _frame(snapshot, config, tmp_path, state={"explanation_target": next_target("work")})
    assert listed == default


def test_english_next_count_line_and_list(tmp_path):
    from foragent.watch_sections.lane_blocks import next_target

    snapshot, config = _queued("en")
    frame, _, _ = _frame(snapshot, config, tmp_path, state={})
    assert "next 3 ▸ click for the list" in frame
    opened, _, _ = _frame(snapshot, config, tmp_path, state={"explanation_target": next_target("work")})
    assert all(f"#{number} Task {number}" in opened for number in QUEUED), opened


def test_the_acorn_says_review_and_the_click_bubble_says_the_round(tmp_path):
    snapshot, config = single_worker(phase="reviewing")
    snapshot = replace(snapshot, review_rounds={101: 6})
    config = replace(config, language="ko")
    frame, _, _ = _frame(snapshot, config, tmp_path, state={})
    assert "#101 리뷰▌" in frame and "6번째" not in frame
    opened, _, _ = _frame(snapshot, config, tmp_path, state={"explanation_target": ("acorn", 101)})
    rows = [line for line in opened.splitlines() if "6번째" in line]
    assert len(rows) == 1 and "│" in rows[0], opened


def test_a_queued_question_stays_an_acorn_and_only_turn_waiting_work_is_counted(tmp_path):
    from foragent.watch_sections.lane_blocks import next_target

    snapshot, config = _queued()
    snapshot = replace(snapshot, questions={103: "person"})
    frame, _, _ = _frame(snapshot, config, tmp_path, state={})
    assert "#103 질문▌" in frame, f"the person's turn is never hidden in the count:\n{frame}"
    assert "다음 2개 ▸ 누르면 목록" in frame and "#104" not in frame and "#105" not in frame, frame
    opened, _, _ = _frame(snapshot, config, tmp_path, state={"explanation_target": next_target("work")})
    listed = [line for line in opened.splitlines() if "│" in line and "Task 10" in line]
    assert [n for n in QUEUED if any(f"#{n} Task" in line for line in listed)] == [104, 105], opened


def test_a_click_on_a_listed_issue_opens_its_own_explanation(tmp_path):
    from foragent.watch_sections.lane_blocks import next_target

    snapshot, config = _queued()
    state = {}
    _click(next_target("work"), snapshot, config, tmp_path, state)
    _click(("acorn", 104), snapshot, config, tmp_path, state)
    assert state["explanation_target"] == ("acorn", 104)
    opened, _, _ = _frame(snapshot, config, tmp_path, state=state)
    bubble = [line for line in opened.splitlines() if "│" in line]
    assert any("#104 Task 104" in line for line in bubble), opened
    assert not any("#103 Task 103" in line for line in bubble), "the list gave way to #104's explanation"
    # The list's title still closes it.
    _click(next_target("work"), snapshot, config, tmp_path, state)
    _click(next_target("work"), snapshot, config, tmp_path, state)
    assert "explanation_target" not in state


def test_up_and_down_reach_every_folded_issue_and_enter_explains_it(tmp_path):
    from foragent import watch

    snapshot, config = _queued()
    state = {}
    frame, _, _ = _frame(snapshot, config, tmp_path, state=state)
    count_row = next(y for y, line in enumerate(frame.splitlines()) if "다음 3개" in line)
    reached = []
    for _ in range(4):
        watch.choose(state, 1)
        _frame(snapshot, config, tmp_path, state=state)
        reached.append((watch.selected(state), watch.highlighted_row(state)))
    assert reached == [(103, count_row), (104, count_row), (105, count_row), (105, count_row)]
    shown, _, _ = _frame(snapshot, config, tmp_path, state=state)
    assert "다음 3개 ▸ 누르면 목록 · #105" in shown, "the count line names the chosen issue"
    from foragent.state import StateStore

    watch.handle_key("enter", StateStore(tmp_path / "state"), config, state=state)
    assert state["explanation_target"] == ("acorn", 105)
    opened, _, _ = _frame(snapshot, config, tmp_path, state=state)
    assert any("#105 Task 105" in line and "│" in line for line in opened.splitlines()), opened


def test_side_by_side_acorns_are_each_a_keyboard_stop(tmp_path):
    from foragent import watch
    from foragent.state import StateStore

    snapshot, config = _queued()
    snapshot = replace(snapshot, questions={103: "person"})
    state = {}
    frame, _, _ = _frame(snapshot, config, tmp_path, state=state)
    row = next(line for line in frame.splitlines() if "#101 구현▌" in line)
    assert "#103 질문▌" in row, "the queued question stands beside the working acorn"
    watch.choose(state, 1)
    assert watch.selected(state) == 103
    watch.handle_key("enter", StateStore(tmp_path / "state"), config, state=state)
    assert state["explanation_target"] == ("acorn", 103)


def _overflow(tmp_path):
    """English pixel scene at 40 columns: working #101 and the person's questions #103 and #104;
    #104 does not fit beside them and is counted in '…1'."""
    snapshot, config = single_worker(phase="implementing")
    snapshot.status["lanes"]["work"]["queued"] = [{"number": n, "title": f"Task {n}"} for n in (103, 104)]
    return replace(snapshot, questions={103: "person", 104: "person"}), replace(config, language="en")


def test_an_acorn_counted_in_the_overflow_opens_from_a_click_on_the_count(tmp_path):
    from foragent.watch_sections.lane_blocks import more_target

    snapshot, config = _overflow(tmp_path)
    state = {}
    frame, layout, raw = _frame(snapshot, config, tmp_path, state=state, width=40)
    assert "…1" in frame and "#104" not in frame, frame
    _click(more_target("work", [104]), snapshot, config, tmp_path, state, width=40)
    listed, _, _ = _frame(snapshot, config, tmp_path, state=state, width=40)
    rows = [line for line in listed.splitlines() if "│" in line]
    assert any("work: 1 more" in line for line in rows) and any("#104 Task 104" in line for line in rows), listed
    _click(("acorn", 104), snapshot, config, tmp_path, state, width=40)
    assert state["explanation_target"] == ("acorn", 104)
    opened, _, _ = _frame(snapshot, config, tmp_path, state=state, width=40)
    assert any("#104 Task 104" in line and "│" in line for line in opened.splitlines()), opened


def test_an_acorn_counted_in_the_overflow_is_a_keyboard_stop(tmp_path):
    from foragent import watch
    from foragent.state import StateStore

    snapshot, config = _overflow(tmp_path)
    state = {}
    frame, _, _ = _frame(snapshot, config, tmp_path, state=state, width=40)
    more_row = next(y for y, line in enumerate(frame.splitlines()) if "…1" in line)
    reached = []
    for _ in range(3):
        watch.choose(state, 1)
        _frame(snapshot, config, tmp_path, state=state, width=40)
        reached.append(watch.selected(state))
    assert reached == [103, 104, 104] and watch.highlighted_row(state) == more_row
    watch.handle_key("enter", StateStore(tmp_path / "state"), config, state=state)
    assert state["explanation_target"] == ("acorn", 104)

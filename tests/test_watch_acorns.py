"""Folding, scroll bounds and the key map of the clickable lane rows (#170)."""

from dataclasses import replace
from datetime import timedelta
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_squirrel_running import plain_frame, running_snapshot  # noqa: E402

from foragent import watch  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker  # noqa: E402


@pytest.fixture(autouse=True)
def emoji_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)


def lane_case(*, running=True, queued=(4, 5, 6), done=(1, 2), merged=None, blocked=False):
    """One lane 'work' with finished, running and waiting issues."""
    snapshot, config = running_snapshot()
    tracker = FakeTracker()
    finished = []
    for number in done:
        tracker.add_issue(number, f"Done {number}", state="closed")
        finished.append(tracker.get_issue(number))
    row = dict(snapshot.status["lanes"]["work"])
    state = "blocked" if blocked else "working" if running else "idle"
    row.update(issue=3 if running or blocked else None, title="Current", state=state,
               running=[{"number": 3, "title": "Current"}] if running else [],
               queued=[{"number": n, "title": f"Task {n}"} for n in queued])
    status = {**snapshot.status, "lanes": {"work": row},
              "active_runs": [{"number": 3, "lane": "work", "attempts": 1}] if running else []}
    snapshot = replace(snapshot, status=status, done={"work": finished}, merged_at=merged or {},
                       completed=len(done), total=len(done) + 1 + len(queued))
    return snapshot, replace(config, lanes={"work": config.lanes["work"]})


def whole(snapshot, config, tmp_path, state):
    """The whole frame without the plain 24-row budget, as the live view scrolls it."""
    lines = watch._frame(snapshot, config, tmp_path, tick=0, width=100, selected=None,
                         now=snapshot.captured_at, height=None, state=state)
    return "\n".join("".join(value for value, _ in line).rstrip() for line in lines)


def numbers(frame):
    """Issue numbers on the lane rows, top to bottom."""
    return [int(found[1]) for line in plain_frame(frame).splitlines()
            if line.startswith(("🐿", "⏳", "💤", "✋", "🔍")) and (found := re.search(r"#(\d+)", line))]


def test_folded_lane_keeps_last_finished_running_and_next_waiting(tmp_path):
    snapshot, config = lane_case()
    frame = whole(snapshot, config, tmp_path, {})
    assert numbers(frame) == [2, 3, 4]
    assert "▸ 3개 더" in frame  # finished #1 and waiting #5, #6


def test_last_finished_follows_merge_time_not_list_order(tmp_path):
    base = running_snapshot()[0].captured_at
    snapshot, config = lane_case(merged={1: base, 2: base - timedelta(hours=1)})
    assert numbers(whole(snapshot, config, tmp_path, {}))[0] == 1


def test_unfolded_lane_lists_everything_then_fold_line(tmp_path):
    snapshot, config = lane_case()
    state = {"unfolded": {"work"}}
    frame = whole(snapshot, config, tmp_path, state)
    assert numbers(frame) == [1, 2, 3, 4, 5, 6]
    assert "▾ 접기" in frame and "▸" not in frame
    assert state["issue_lanes"] == {n: "work" for n in range(1, 7)}


def test_idle_lane_folds_to_its_summary_and_unfolds_finished_rows(tmp_path):
    snapshot, config = lane_case(running=False, queued=())
    folded = plain_frame(whole(snapshot, config, tmp_path, {}))
    assert numbers(folded) == [] and "▸ 2개 더" in folded
    opened = plain_frame(whole(snapshot, config, tmp_path, {"unfolded": {"work"}}))
    rows = [line for line in opened.splitlines() if line.startswith("💤") and "#" in line]
    assert [int(re.search(r"#(\d+)", line)[1]) for line in rows] == [1, 2]
    assert all(line.startswith("💤") and "끝났어요" in line and "🌰" * 5 in line for line in rows)


def test_no_toggle_line_when_nothing_is_hidden(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=(2,))
    frame = whole(snapshot, config, tmp_path, {})
    assert numbers(frame) == [2, 3, 4] and "▸" not in frame and "▾" not in frame


def test_blocked_issue_counts_as_running_when_folded(tmp_path):
    snapshot, config = lane_case(running=False, blocked=True)
    assert numbers(whole(snapshot, config, tmp_path, {})) == [2, 3, 4]


def test_a_short_plain_frame_drops_folded_extras_before_other_sections(tmp_path):
    snapshot, config = lane_case(running=False, blocked=True)
    assert numbers(whole(snapshot, config, tmp_path, {})) == [2, 3, 4]
    short = watch.render_frame(snapshot, config, tmp_path, width=100, state={})
    assert numbers(short) == [3, 4] and "▸" not in short
    assert len(short.splitlines()) <= 24


def test_scroll_stays_within_the_last_full_screen(tmp_path):
    state = {"length": 50, "height": 20}
    watch.scroll(state, 100)
    assert state["scroll"] == 31  # rows 32–50 fill the 19 rows above the position line
    watch.scroll(state, -100)
    assert state["scroll"] == 0
    state = {"length": 10, "height": 20}
    watch.scroll(state, 5)
    assert state["scroll"] == 0, "a frame that fits does not scroll"


def test_scroll_before_any_screen_is_bounded_by_the_last_frame(tmp_path):
    state = {"length": 7}
    watch.scroll(state, 100)
    assert state["scroll"] == 6
    assert watch.scroll_limit({}) == 0


def test_window_shows_position_line_only_when_taller_than_the_screen(tmp_path):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    state = {"unfolded": {"work"}, "scroll": 999}
    full = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, state=state)).splitlines()
    shown = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)).splitlines()
    assert state["scroll"] == len(full) - 11
    assert shown[:-1] == full[-11:]
    assert shown[-1].startswith(f"{len(full) - 10}–{len(full)}/{len(full)}줄")
    small = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, height=200, state={}))
    assert "줄 ·" not in small and "Option" not in small


def test_key_map(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    state = {"length": 30, "height": 10, "rows": [(1, 2), (2, 3), (3, 4)],
             "issue_lanes": {2: "work", 3: "work", 4: "work"}, "lanes": ["work"]}
    assert watch.handle_key("j", store, config, state=state) is False and state["scroll"] == 1
    assert watch.highlighted(state) == 2
    watch.handle_key("j", store, config, state=state)
    assert watch.highlighted(state) == 3
    watch.handle_key("f", store, config, state=state)
    assert state["unfolded"] == {"work"}
    watch.handle_key("f", store, config, state=state)
    assert state["unfolded"] == set()
    watch.handle_key("a", store, config, state=state)
    assert state["unfolded"] == {"work"}
    watch.handle_key("a", store, config, state=state)
    assert state["unfolded"] == set()
    watch.handle_key("x", store, config, state=state)
    assert state.get("issue") is None, "x without a box does nothing"
    watch.handle_key("enter", store, config, state=state)
    assert state["issue"] == 3 and state["store"] is store
    for key in ("enter", "f", "a"):
        before = dict(state)
        watch.handle_key(key, store, config, state=state)
        assert state == before, f"{key} is ignored while a box is open"
    assert watch.handle_key("esc", store, config, state=state) is False and state["issue"] is None
    assert watch.handle_key("esc", store, config, state=state) is True, "Esc quits when no box is open"
    assert watch.handle_key("q", store, config, state=state) is True


def test_enter_and_f_do_nothing_without_an_issue_row_on_screen(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    state = {"length": 30, "height": 10, "scroll": 5, "rows": [(1, 2), (20, 3)], "issue_lanes": {2: "work"}}
    assert watch.highlighted(state) is None
    watch.handle_key("enter", store, config, state=state)
    watch.handle_key("f", store, config, state=state)
    assert state.get("issue") is None and "unfolded" not in state


def test_wheel_scrolls_and_a_click_on_another_issue_switches_the_box(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    layout = {"hits": [(0, 1, 40, 2, ("acorn", 2)), (0, 2, 40, 3, ("acorn", 3))]}
    state = {"length": 30, "height": 10, "scroll": 4}
    watch.handle_mouse("wheel-down", 0, 1, layout, store, config, state=state)
    assert state["scroll"] == 5 and state.get("issue") is None
    watch.handle_mouse("wheel-up", 0, 1, layout, store, config, state=state)
    assert state["scroll"] == 4
    watch.handle_mouse("left", 0, 1, layout, store, config, state=state)
    assert state["issue"] == 2
    watch.handle_mouse("left", 0, 2, layout, store, config, state=state)
    assert state["issue"] == 3


def test_issue_box_phase_words_and_missing_sections(tmp_path):
    store = StateStore(tmp_path)
    config = replace(lane_case()[1], language="ko", repo=None)
    tracker = FakeTracker()
    tracker.add_issue(7, "Queued task", "no headings here", labels=["approved", "lane:work"])
    tracker.add_issue(8, "Shipped", "## Goal\nShip it.\n", labels=["lane:work"], state="closed")
    for number in (7, 8):
        store.record_issue(tracker.get_issue(number), config.lanes)
    queued = "\n".join(watch.issue_box(7, store, config))
    assert "차례 기다리는 중" in queued and "목표" not in queued and "완료 조건" not in queued
    assert "https://" not in queued, "no repository, no link"
    shipped = "\n".join(watch.issue_box(8, store, config))
    assert "끝났어요" in shipped and "Ship it." in shipped and "완료 조건" not in shipped


def test_osc8_and_screen_sequences():
    assert watch.osc8("u", "#1") == "\x1b]8;;u\x1b\\#1\x1b]8;;\x1b\\"
    assert watch.terminal_setup_sequences() == ["\x1b[?1049h"]
    assert watch.terminal_restore_sequences() == ["\x1b[?1049l"]

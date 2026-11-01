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
    """Issue numbers on the lane acorns (sideways ◖█#N▌, or ASCII [#N]), top to bottom, left to right."""
    return [int(number) for line in plain_frame(frame).splitlines()
            for number in re.findall(r"(?:[◖◜◟]█|\[)#(\d+)", line)]

def bubble_of(frame):
    """The orchestrator bubble's six body rows (the shared acceptance helper)."""
    from test_lane_m2_contract import bubble

    return bubble(frame)


def box_frame(config, tmp_path, state, width):
    """The legacy issue box through its retained section API (the lane scene no longer shows it)."""
    from foragent.watch_sections import acorns

    snapshot = lane_case()[0]
    lines = acorns.render(snapshot, config, tmp_path, tick=0, width=width, selected=None,
                          now=snapshot.captured_at, top=0, hits=[], marks=[], state=state)
    return "\n".join("".join(value for value, _ in line).rstrip() for line in lines)



def test_folded_lane_keeps_last_finished_running_and_next_waiting(tmp_path):
    snapshot, config = lane_case()
    frame = whole(snapshot, config, tmp_path, {})
    # The lane scene: the running acorn, then the whole queue; merged #1 and #2 are no lane rows.
    assert numbers(frame) == [3, 4, 5, 6]
    assert "▸" not in frame and "#1" not in frame and "#2" not in frame


def test_last_finished_follows_merge_time_not_list_order(tmp_path):
    base = running_snapshot()[0].captured_at
    snapshot, config = lane_case(merged={1: base, 2: base - timedelta(hours=1)})
    layout = {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, layout=layout)
    # Today's merges are the bubble's summary, newest first, never lane rows or invented acorn hits.
    assert "방금 #1, #2를 머지했어요." in bubble_of(frame)
    assert numbers(frame) == [3, 4, 5, 6]
    assert not {("acorn", 1), ("acorn", 2)} & {target for *_, target in layout["hits"]}


def test_unfolded_lane_lists_everything_then_fold_line(tmp_path):
    snapshot, config = lane_case()
    state = {"unfolded": {"work"}}
    frame = whole(snapshot, config, tmp_path, state)
    # Every lane always shows all its acorns: there is nothing left to unfold or fold.
    assert numbers(frame) == [3, 4, 5, 6] and frame == whole(snapshot, config, tmp_path, {})
    assert "▾" not in frame and "▸" not in frame
    # The current row and the queue row are the lane's issue rows (first acorn of each row).
    assert {dict(state["rows"])[y]: lane for y, lane in state["row_lanes"].items()} == {3: "work", 4: "work"}


def test_idle_lane_folds_to_its_summary_and_unfolds_finished_rows(tmp_path):
    snapshot, config = lane_case(running=False, queued=())
    for state in ({}, {"unfolded": {"work"}}):
        lines = plain_frame(whole(snapshot, config, tmp_path, state)).splitlines()
        # An idle lane sleeps in the shared block by its name; its merges are no rows.
        assert numbers("\n".join(lines)) == [] and not any("▸" in line or "▾" in line for line in lines)
        heading = next(y for y, line in enumerate(lines) if y and line[14:] == "쉬고 있어요")
        assert lines[heading + 1][14:] == "work"


def test_no_toggle_line_when_nothing_is_hidden(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=(2,))
    frame = whole(snapshot, config, tmp_path, {})
    assert numbers(frame) == [3, 4] and "▸" not in frame and "▾" not in frame


def test_blocked_issue_counts_as_running_when_folded(tmp_path):
    snapshot, config = lane_case(running=False, blocked=True)
    lines = plain_frame(whole(snapshot, config, tmp_path, {})).splitlines()
    # The blocked issue stays a current acorn (grey 대기) and the lane says it waits.
    assert numbers("\n".join(lines)) == [3, 4, 5, 6]
    current = next(y for y, line in enumerate(lines) if "◖█#3 대기▌" in line)
    assert lines[current + 1][14:] == "순서를 기다려요"


def test_a_short_plain_frame_drops_folded_extras_before_other_sections(tmp_path):
    snapshot, config = lane_case(running=False, blocked=True)
    assert numbers(whole(snapshot, config, tmp_path, {})) == [3, 4, 5, 6]
    whole_frame = watch.render_frame(snapshot, config, tmp_path, width=100, state={})
    assert numbers(whole_frame) == [3, 4, 5, 6]
    lines = watch._frame(snapshot, config, tmp_path, tick=0, width=100, selected=None,
                         now=snapshot.captured_at, height=8, state={})
    short = "\n".join("".join(value for value, _ in line).rstrip() for line in lines)
    # The lane blocks have no compact form: an explicit height drops nothing (the viewport scrolls).
    assert short == whole(snapshot, config, tmp_path, {})


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
    """Pinned header and footer; the body slice, then the position line over the body rows."""
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    state = {"unfolded": {"work"}, "scroll": 999}
    full = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, state=state)).splitlines()
    shown = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)).splitlines()
    body = full[1:-2]
    assert state["scroll"] == len(body) - 8
    assert shown[0] == full[0] and shown[-2:] == full[-2:], "header, usage and hints stay put"
    assert shown[1:-3] == body[-8:], "only the body scrolls"
    assert full[-2].startswith("Claude") and full[-1].endswith("q 끝내기")
    assert shown[-3].startswith(f"{len(body) - 7}–{len(body)}/{len(body)}줄")
    small = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, height=200, state={}))
    assert "줄 ·" not in small and "Option" not in small


def test_key_map(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    state = {"length": 30, "height": 10, "rows": [(1, 2), (2, 3), (3, 4)],
             "row_lanes": {1: "work", 2: "work", 3: "work"}, "lanes": ["work"]}
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
    assert state.get("issue") is None and "explanation_target" not in state, "x with nothing open does nothing"
    # Enter explains the highlighted acorn in the bubble: no box, no scroll change.
    watch.handle_key("enter", store, config, state=state)
    assert state["explanation_target"] == ("acorn", 3) and state.get("issue") is None and state["scroll"] == 2
    watch.handle_key("enter", store, config, state=state)
    assert "explanation_target" not in state, "Enter on the explained acorn closes it"
    watch.handle_key("enter", store, config, state=state)
    assert watch.handle_key("esc", store, config, state=state) is False
    assert "explanation_target" not in state and state["scroll"] == 2, "Esc closes the explanation in place"
    assert watch.handle_key("esc", store, config, state=state) is True, "Esc quits when nothing is open"
    assert watch.handle_key("q", store, config, state=state) is True


def test_enter_and_f_do_nothing_without_an_issue_row_on_screen(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    state = {"length": 30, "height": 10, "scroll": 5, "rows": [(1, 2), (20, 3)], "row_lanes": {1: "work"}}
    assert watch.highlighted(state) is None
    watch.handle_key("enter", store, config, state=state)
    watch.handle_key("f", store, config, state=state)
    assert state.get("issue") is None and "unfolded" not in state


def test_wheel_scrolls_and_a_click_on_another_issue_switches_the_box(tmp_path):
    store, config = StateStore(tmp_path), lane_case()[1]
    layout = {"hits": [(0, 1, 40, 2, ("acorn", 2)), (0, 2, 40, 3, ("acorn", 3))]}
    state = {"length": 30, "height": 10, "scroll": 4}
    watch.handle_mouse("wheel-down", 0, 1, layout, store, config, state=state)
    assert state["scroll"] == 5 and "explanation_target" not in state
    watch.handle_mouse("wheel-up", 0, 1, layout, store, config, state=state)
    assert state["scroll"] == 4
    watch.handle_mouse("left", 0, 1, layout, store, config, state=state)
    assert state["explanation_target"] == ("acorn", 2) and state["scroll"] == 4 and state.get("issue") is None
    watch.handle_mouse("left", 0, 2, layout, store, config, state=state)
    assert state["explanation_target"] == ("acorn", 3), "another acorn switches the explanation"
    watch.handle_mouse("left", 0, 2, layout, store, config, state=state)
    assert "explanation_target" not in state and state["scroll"] == 4, "the same acorn closes it"


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


def test_held_issues_beside_a_worker_each_get_a_row(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=())
    row = snapshot.status["lanes"]["work"]
    row["held"] = [{"number": 7, "title": "Blocked one", "state": "blocked"},
                   {"number": 8, "title": "Needs you", "state": "waiting-human"}]
    for state in ({}, {"unfolded": {"work"}}):
        lines = plain_frame(whole(snapshot, config, tmp_path, state)).splitlines()
        assert numbers("\n".join(lines)) == [3, 7, 8, 4]
        current = next(line for line in lines if "◖█#3 작성▌" in line)
        # Held issues share the lane's current acorns: blocked is grey 대기, a person's question red 질문.
        assert "◖█#7 대기▌" in current and "◖█#8 질문▌" in current
        assert not any(line[14:] == "순서를 기다려요" for line in lines), (
            "only the lane's representative in a wait gets the waiting sentence")


def test_status_lists_every_held_issue_of_a_lane(tmp_path):
    from foragent.config import Config
    from foragent.status import collect_lanes
    from foragent.tracker import Issue

    store, config = StateStore(tmp_path), Config(lanes={"work": {}})
    issues = [Issue(n, f"T{n}", "", frozenset(labels), "open") for n, labels in (
        (1, {"approved", "lane:work", "blocked"}), (2, {"approved", "lane:work", "needs-human"}),
        (3, {"approved", "lane:work", "ready"}))]
    groups = {"needs-human": [{"number": 2, "title": "T2"}], "ready": [{"number": 3, "title": "T3"}],
              "running": [], "pr-open": [], "approved-waiting": [{"number": 1, "title": "T1"}],
              "unapproved": []}
    lanes, _ = collect_lanes(issues, groups, {}, store, config, running_snapshot()[0].captured_at)
    assert [(item["number"], item["state"]) for item in lanes["work"]["held"]] == [
        (1, "blocked"), (2, "waiting-human")]
    assert [item["number"] for item in lanes["work"]["queued"]] == [3]


def test_queued_rows_do_not_take_the_lanes_phase(tmp_path):
    snapshot, config = lane_case(running=False, queued=(4, 5), done=())
    snapshot.status["lanes"]["work"]["phase"] = "merging"
    frame = plain_frame(whole(snapshot, config, tmp_path, {}))
    assert "◖█#4▌" in frame and "◖█#5▌" in frame and "#4 머지" not in frame


def test_a_frame_draws_the_same_rows_with_or_without_view_state(tmp_path):
    """Class: content must not depend on whether a caller passed view state."""
    snapshot, config = lane_case()
    printed = watch.render_frame(snapshot, config, tmp_path, width=100)
    live = watch.render_frame(snapshot, config, tmp_path, width=100, state={})
    assert printed == live
    assert numbers(printed) == [3, 4, 5, 6] and "▸" not in printed
    assert len(printed.splitlines()) <= 24


def test_a_finished_orders_idle_lane_still_offers_its_finished_rows(tmp_path):
    """Lane acorns replace the folded finished rows: a finished order's idle lane just sleeps."""
    snapshot, config = lane_case(running=False, queued=(), done=tuple(range(1, 13)))
    snapshot = replace(snapshot, total=snapshot.completed)
    for state in (None, {}, {"unfolded": {"work"}}):
        frame = whole(snapshot, config, tmp_path, state)
        assert numbers(frame) == [] and "▸" not in frame and "▾" not in frame
        assert "지금은 할 일이 없어요." in bubble_of(frame)


def test_issue_box_shares_the_rows_state_and_lane(tmp_path):
    store = StateStore(tmp_path)
    config = replace(lane_case()[1], language="ko", repo=None,
                     lanes={"work": {"title": "work"}, "docs": {"title": "docs"}})
    tracker = FakeTracker()
    tracker.add_issue(5, "Has PR", "", labels=["approved", "lane:work"])
    tracker.add_issue(6, "Moved", "", labels=["approved", "running", "lane:work"])
    for number in (5, 6):
        store.record_issue(tracker.get_issue(number), config.lanes)
    store.update_local_issue(5, pr_number=50)
    store.start_run(6, branch="b", worktree="w", lane="docs")
    assert "사람 기다리는 중" in "\n".join(watch.issue_box(5, store, config)), "an open PR waits for a person"
    assert "레인: docs" in watch.issue_box(6, store, config), "the run's lane, as in the rows"
    assert "레인: work" in watch.issue_box(6, store, config, "work"), "the row's own lane wins"


def test_completed_order_merges_stay_reachable_by_click_and_keys(tmp_path):
    snapshot, config = lane_case(running=False, queued=(), done=(1, 2))
    snapshot = replace(snapshot, total=snapshot.completed, merged_at={
        1: snapshot.captured_at - timedelta(minutes=5), 2: snapshot.captured_at})
    store, state, layout = StateStore(tmp_path), {}, {}
    folded = "\n".join("".join(value for value, _ in line) for line in watch._frame(
        snapshot, config, tmp_path, tick=0, width=100, selected=None, now=snapshot.captured_at,
        height=None, state=state, layout=layout))
    # The newest merges are a bubble summary; they get no invented acorn hits or issue rows.
    assert "방금 #2, #1를 머지했어요." in bubble_of(folded)
    assert not {("acorn", 1), ("acorn", 2)} & {target for *_, target in layout["hits"]}
    assert state["rows"] == []
    before = whole(snapshot, config, tmp_path, state)
    watch.handle_key("a", store, config, state=state)
    assert whole(snapshot, config, tmp_path, state) == before, "unfolding changes nothing any more"
    watch.handle_key("enter", store, config, state=state)
    assert "explanation_target" not in state and state.get("issue") is None


def test_missing_times_show_a_dash_on_every_row_kind(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=())
    row = snapshot.status["lanes"]["work"]
    row.update(elapsed_seconds=None)
    row["held"] = [{"number": 7, "title": "Blocked one", "state": "blocked"}]
    assert numbers(whole(snapshot, config, tmp_path, {})) == [3, 7, 4]
    # Times live in the acorn explanation; without a recorded time it shows none (no dash, no zero).
    for number in (3, 7, 4):
        words = bubble_of(whole(snapshot, config, tmp_path, {"explanation_target": ("acorn", number)}))
        assert words.startswith(f"#{number} ") and "째 하고 있어요" not in words, words


def test_a_stopped_run_keeps_its_phase_in_row_and_box(tmp_path):
    snapshot, config = lane_case(running=False, queued=(), done=())
    row = snapshot.status["lanes"]["work"]
    row["held"] = [{"number": 9, "title": "Stopped", "state": "working"}]
    snapshot = replace(snapshot, run_phases={9: "reviewing"})
    assert "◖█#9 리뷰▌" in plain_frame(whole(snapshot, config, tmp_path, {}))
    opened = whole(snapshot, config, tmp_path, {"explanation_target": ("acorn", 9)})
    assert "다른 AI가 리뷰하고 있어요" in bubble_of(opened)

    store = StateStore(tmp_path / "state")
    config = replace(config, language="ko", repo=None)
    tracker = FakeTracker()
    tracker.add_issue(9, "Stopped", "", labels=["approved", "running", "lane:work"])
    tracker.add_issue(10, "Reopened", "", labels=["approved", "ready", "lane:work"])
    for number in (9, 10):
        store.record_issue(tracker.get_issue(number), config.lanes)
    store.start_run(9, branch="b9", worktree="w9", lane="work")
    store.update_run(9, phase="reviewing")
    store.finish_run(9, outcome="failed", detail="stopped")
    assert "지금: 리뷰 받는 중" in watch.issue_box(9, store, config)
    store.start_run(10, branch="b10", worktree="w10", lane="work")
    store.finish_run(10, outcome="merged")
    assert "지금: 차례 기다리는 중" in watch.issue_box(10, store, config), "a reopened issue is not done"


def test_links_restore_the_cursor_curses_expects(tmp_path, monkeypatch):
    import curses
    from io import StringIO
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal

    snapshot, config = lane_case()
    screen = Mock()
    screen.getmaxyx.return_value = (40, 100)
    screen.getch.return_value = ord("q")
    output = StringIO()
    monkeypatch.setattr(watch.sys, "stdout", output)
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path,
                             lambda: snapshot.captured_at, StateStore(tmp_path))
    written = output.getvalue()
    assert "\x1b]8;;https://github.com/owner/repo/issues/3" in written
    # Every cursor move for a link sits between one save and its restore.
    assert written.startswith("\x1b7") and written.endswith("\x1b8")
    assert written.count("\x1b7") == written.count("\x1b8") == 1


def test_footers_list_the_new_keys_from_their_own_feature_file(tmp_path):
    from foragent.messages import acorns, squirrel, watch as shared

    snapshot, config = lane_case()
    # The hint row (lane_footer) is the last row, directly under the usage row.
    assert watch.render_frame(snapshot, config, tmp_path, width=200).splitlines()[-1] == (
        "도토리를 누르면 설명해요 · s 설정 · m 사용량 모드 · q 끝내기")
    assert "acorns_keys" in acorns.MESSAGES["en"]
    assert all("j/k move" not in value for module in (squirrel, shared)
               for table in module.MESSAGES.values() for value in table.values())


def test_hits_use_screen_rows_after_a_height_render(tmp_path):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    state, whole_layout, layout = {"unfolded": {"work"}}, {}, {}
    watch.render_frame(snapshot, config, tmp_path, width=100, state=state, layout=whole_layout)
    state["scroll"] = 5
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state, layout=layout)
    # Screen rows map to F rows through row_map: pinned rows stay, the body scrolls by 5.
    screen = {row: y for y, row in layout["row_map"].items()}
    assert screen[0] == 0 and screen[state["pinned_rows"]["footer"][-1]] == 11
    expected = [(x0, screen[row], x1, screen[row] + 1, target)
                for x0, y0, x1, y1, target in whole_layout["hits"] for row in range(y0, y1) if row in screen]
    assert layout["hits"] == expected and expected
    # Header row 0, body rows 1-8, the position row 9, usage 10 and hints 11.
    assert not any(y0 <= 9 < y1 for _, y0, _, y1, _ in layout["hits"]), "the position row has no hits"
    x, y = next((x0, y0) for x0, y0, _, _, target in layout["hits"] if target == ("acorn", 3))
    watch.handle_mouse("left", x, y, layout, StateStore(tmp_path), config, state=state)
    assert state["explanation_target"] == ("acorn", 3) and state["scroll"] == 5, (
        "a screen-row click explains the acorn shown on that row, in place")


def live_paint(tmp_path, monkeypatch, snapshot, config, *, height, unfolded=None):
    import curses
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal

    screen = Mock()
    screen.getmaxyx.return_value = (height, 100)
    screen.getch.return_value = ord("q")
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    monkeypatch.setattr(watch.sys, "stdout", __import__("io").StringIO())
    if unfolded:
        real = watch._frame
        monkeypatch.setattr(watch, "_frame", lambda *a, state, **k: real(
            *a, state=state.setdefault("unfolded", set()).update(unfolded) or state, **k))
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path,
                             lambda: snapshot.captured_at, StateStore(tmp_path))
    rows: dict[int, str] = {}
    reverse = set()
    for call in screen.addstr.call_args_list:
        y, x, value, attribute = call.args
        rows[y] = rows.get(y, "") + value
        if attribute & curses.A_REVERSE:
            reverse.add(y)
    return rows, reverse


def test_live_view_paints_exactly_render_frames_slice(tmp_path, monkeypatch):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    rows, _ = live_paint(tmp_path, monkeypatch, snapshot, config, height=15, unfolded={"work"})
    expected = watch.render_frame(snapshot, config, tmp_path, width=100, height=15,
                                  state={"unfolded": {"work"}}).splitlines()
    assert [rows.get(y, "").rstrip() for y in range(15)] == expected
    tall, _ = live_paint(tmp_path, monkeypatch, snapshot, config, height=60, unfolded={"work"})
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, state={"unfolded": {"work"}}).splitlines()
    assert len(frame) < 60
    assert [tall.get(y, "").rstrip() for y in range(len(frame) - 2)] == frame[:-2], (
        "a tall screen paints F itself above its pinned footer, no extra sections")
    assert [tall[58].rstrip(), tall[59].rstrip()] == frame[-2:], "usage and hints sit on the bottom rows"
    assert not any(text.strip() for y, text in tall.items() if len(frame) - 2 <= y < 58)


def test_only_the_first_visible_issue_row_is_highlighted(tmp_path, monkeypatch):
    """Class: selection by row position, not issue identity (several acorn rows)."""
    snapshot, config = lane_case()
    rows, reverse = live_paint(tmp_path, monkeypatch, snapshot, config, height=60)
    # The current row and the queue row are both issue rows; only the first one is highlighted.
    assert sum("◖█#" in text for text in rows.values()) == 2
    assert len(reverse) == 1 and "#3 작성" in rows[min(reverse)]
    # Held issues add acorns to the current row, not more highlighted rows.
    snapshot.status["lanes"]["work"]["held"] = [{"number": 7, "title": "Blocked one", "state": "blocked"}]
    state = {}
    watch._frame(snapshot, config, tmp_path, tick=0, width=100, selected=None, now=snapshot.captured_at, state=state)
    assert [number for _, number in state["rows"]] == [3, 4]
    rows, reverse = live_paint(tmp_path, monkeypatch, snapshot, config, height=60)
    assert len(reverse) == 1 and "#7 대기" in rows[min(reverse)]


def test_closing_the_box_keeps_the_rows_scroll(tmp_path):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    store, state = StateStore(tmp_path), {"unfolded": {"work"}}
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    for _ in range(7):
        watch.handle_key("j", store, config, state=state)
    assert state["scroll"] == 7
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    watch.handle_key("enter", store, config, state=state)
    assert state["explanation_target"] == ("acorn", 3) and state["scroll"] == 7, "explaining keeps the scroll"
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    for key in ("x", "x"):
        watch.handle_key(key, store, config, state=state)
        assert "explanation_target" not in state and state["scroll"] == 7, "x closes once, then does nothing"
    watch.handle_mouse("left", 0, 0, {"hits": [(0, 0, 9, 1, ("acorn", 9))]}, store, config, state=state)
    assert state["explanation_target"] == ("acorn", 9) and state["scroll"] == 7
    watch.handle_mouse("left", 0, 0, {"hits": [(0, 0, 9, 1, ("acorn", 9))]}, store, config, state=state)
    assert "explanation_target" not in state and state["scroll"] == 7, "a click that opens then closes keeps it too"


VERBATIM_BODY = (
    "## Goal\n    indented  first line\nsecond   line\n"
    "## Done when\n```sh\nprintf 'a  b'\ncat <<'EOF'\n    kept  indent\n\tTabbed\nEOF\n```\n"
)


def box_case(tmp_path, body=VERBATIM_BODY):
    store = StateStore(tmp_path / "state")
    config = replace(lane_case()[1], language="en", repo=None)
    tracker = FakeTracker()
    tracker.add_issue(5, "Two  spaces  title", body, labels=["approved", "lane:work"])
    store.record_issue(tracker.get_issue(5), config.lanes)
    return store, config


def test_issue_box_keeps_whitespace_of_goal_title_and_commands(tmp_path):
    """Class: whitespace corruption of text shown verbatim from the local record."""
    store, config = box_case(tmp_path)
    lines = watch.issue_box(5, store, config)
    for expected in ("#5 Two  spaces  title", "      indented  first line", "  second   line",
                     "  printf 'a  b'", "      kept  indent", "          Tabbed"):
        assert expected in lines, (expected, lines)
    state = {"issue": 5, "store": store}
    frame = box_frame(config, tmp_path, state, 100)
    assert "│  printf 'a  b'" in frame and "│      kept  indent" in frame and "│      indented  first line" in frame


def test_a_wrapped_box_line_joins_back_to_the_exact_text(tmp_path):
    from foragent.watch_sections.acorns import _verbatim_wrap

    line = "  run --flag  'two  spaces'  다람쥐도토리 " + "x" * 30
    for width in (2, 7, 12, 20):
        pieces = _verbatim_wrap(line, width)
        assert "".join(pieces) == line
        assert all(watch._cells(piece) <= width for piece in pieces)
    store, config = box_case(tmp_path, "## Done when\n```sh\n" + line + "\n```\n")
    frame = box_frame(config, tmp_path, {"issue": 5, "store": store}, 24)
    inside = [row[1:-1] for row in frame.splitlines() if row.startswith("│")]
    assert "".join(inside).find("  " + line) >= 0, "the wrapped rows hold every character in order"


def test_issue_box_drops_terminal_controls_but_not_spaces(tmp_path):
    store, config = box_case(tmp_path, "## Done when\n```sh\necho  \x1b[31mred\x07\n```\n")
    assert "  echo  [31mred" in watch.issue_box(5, store, config)


@pytest.mark.parametrize("sizes, first_key", [
    (((40, 100), (45, 100)), "resize"),  # curses clears and repaints the same text after a resize
    (((40, 100), (40, 100)), "timeout"),  # or repaints it on its own; the links must come back each time
])
def test_links_are_written_after_every_refresh_even_when_text_is_unchanged(tmp_path, monkeypatch, sizes, first_key):
    """Class: repaint-cache invalidation. No cache, so no repaint can lose the links."""
    import curses
    from io import StringIO
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal

    snapshot, config = lane_case()
    screen = Mock()
    screen.getmaxyx.side_effect = list(sizes)
    screen.getch.side_effect = [curses.KEY_RESIZE if first_key == "resize" else -1, ord("q")]
    output = StringIO()
    monkeypatch.setattr(watch.sys, "stdout", output)
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path,
                             lambda: snapshot.captured_at, StateStore(tmp_path))
    written = output.getvalue()
    link = "\x1b]8;;https://github.com/owner/repo/issues/3\x1b\\#3"
    assert screen.refresh.call_count == 2
    assert written.count("\x1b7") == written.count("\x1b8") == 2
    assert written.count(link) == 2, "each refresh writes the link again"


def final_rows_match_state(frame, state):
    """Every recorded issue row shows its number on that very row of the final frame."""
    lines = plain_frame(frame).splitlines()
    for y, number in state["rows"]:
        assert re.search(rf"#{number}(?!\d)", lines[y]) or lines[y - 1].find(f"#{number}") >= 0, (y, number, lines[y])
    return lines


@pytest.mark.parametrize("case", ["tight", "tight-fits", "completed", "unfolded-completed"])
def test_interaction_data_comes_from_the_final_rows_after_trimming(tmp_path, case):
    """Class: interaction data built once from the final rows, never from a replaced draw."""
    if case == "tight":
        # A 10-row screen: the frame overflows; the lane blocks still draw every acorn.
        snapshot, config = lane_case(running=False, blocked=True)
    elif case == "tight-fits":
        snapshot, config = lane_case(running=False, blocked=True, queued=(), done=(1, 2))
    else:
        snapshot, config = lane_case(running=False, queued=(), done=(1, 2))
        snapshot = replace(snapshot, total=snapshot.completed)
    state = {"unfolded": {"work"}} if case == "unfolded-completed" else {}
    if case.startswith("tight"):
        frame = "\n".join("".join(value for value, _ in line).rstrip() for line in watch._frame(
            snapshot, config, tmp_path, tick=0, width=100, selected=None, now=snapshot.captured_at,
            height=10, state=state))
    else:
        frame = watch.render_frame(snapshot, config, tmp_path, width=100, state=state)
    lines = final_rows_match_state(frame, state)
    marked = dict(state["rows"])
    # Every issue row is an acorn row of its lane, named by the row's first acorn.
    assert set(state["row_lanes"]) == set(marked) and set(state["row_lanes"].values()) <= {"work"}
    for y, number in marked.items():
        assert re.search(rf"[◖◜◟]█#{number}(?!\d)", lines[y]), lines[y]
    if case == "tight":
        assert set(marked.values()) == {3, 4}, "the blocked acorn's row and the queue's row"
    if case == "tight-fits":
        assert set(marked.values()) == {3}, "merges are no issue rows"
    if case in {"completed", "unfolded-completed"}:
        assert marked == {}, "a finished order's merges are the bubble's summary, not issue rows"


def test_completion_rows_open_a_box_but_do_not_fold(tmp_path):
    snapshot, config = lane_case(running=False, queued=(), done=(1, 2))
    snapshot = replace(snapshot, total=snapshot.completed)
    store, state, layout = StateStore(tmp_path), {}, {}
    watch.render_frame(snapshot, config, tmp_path, width=100, state=state, layout=layout)
    # Merges have no rows or acorns in the lane scene: f and Enter find nothing to act on.
    assert state["rows"] == [] and not any(target[0] == "acorn" for *_, target in layout["hits"])
    watch.handle_key("f", store, config, state=state)
    assert "unfolded" not in state, "f does nothing without an issue row"
    watch.handle_key("enter", store, config, state=state)
    assert "explanation_target" not in state and state.get("issue") is None


@pytest.mark.parametrize("height", [0, 1, 2])
def test_a_screen_under_three_rows_counts_as_no_height(tmp_path, height):
    """Class: no forced positive capacity below 4 rows (window, scroll bound, highlight):
    the frame is cut from its top, with no position line and no scrolling (shell)."""
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    store, state = StateStore(tmp_path), {"unfolded": {"work"}}
    whole_frame = watch.render_frame(snapshot, config, tmp_path, width=100, state=dict(state))
    short = watch.render_frame(snapshot, config, tmp_path, width=100, height=height, state=state)
    assert short == "\n".join(whole_frame.splitlines()[:height]) and "Option" not in short, (
        "F from its top, no position line")
    for _ in range(3):
        watch.handle_key("j", store, config, state=state)
    assert state["scroll"] == 0 and watch.scroll_limit(state) == 0
    assert watch.highlighted_row(state) == min(y for y, _ in state["rows"])
    three = watch.render_frame(snapshot, config, tmp_path, width=100, height=3, state={"unfolded": {"work"}})
    assert three.splitlines() == whole_frame.splitlines()[:3] and "Option" not in three


def test_live_view_under_three_rows_paints_the_start_of_f(tmp_path, monkeypatch):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    rows, _ = live_paint(tmp_path, monkeypatch, snapshot, config, height=2, unfolded={"work"})
    expected = watch.render_frame(snapshot, config, tmp_path, width=100, state={"unfolded": {"work"}}).splitlines()
    assert sorted(rows) == [0, 1] and [rows[0].rstrip(), rows[1].rstrip()] == expected[:2]


def test_done_summary_and_more_line_are_plain_text(tmp_path):
    running, config = lane_case()
    state = {}
    frame = whole(running, config, tmp_path, state)
    summary = next(y for y, line in enumerate(frame.splitlines()) if "순서를 기다려요." in line)
    assert summary not in {y for y, _ in state["rows"]}, "the bubble summary is no issue row"
    many, _ = lane_case(queued=tuple(range(4, 60)))
    layout = {}
    frame = watch.render_frame(many, config, tmp_path, width=100, layout=layout)
    lines = plain_frame(frame).splitlines()
    y = next(y for y, line in enumerate(lines) if re.search(r"…\d+$", line.rstrip()))
    x = watch._cells(lines[y][:lines[y].rindex("…")])
    assert watch.hit_test(layout, x, y) is None, "the …N count is plain text"
    # Exact overflow: the visible acorns and the count cover the running one and all 56 queued.
    assert len(numbers(frame)) + int(lines[y].rstrip().rsplit("…", 1)[1]) == 57
    assert {target[1] for *_, target in layout["hits"] if target[0] == "acorn"} == set(numbers(frame))


@pytest.mark.parametrize("unfolded, height", [(set(), 40), ({"work"}, 15)])
def test_a_refresh_error_takes_the_bottom_row(tmp_path, monkeypatch, unfolded, height):
    """The error takes the row above the pinned usage and hint rows: the position row
    while F is taller than the screen, else the last body row (shell, v3.shell.04)."""
    import curses
    from concurrent.futures import Future
    from io import StringIO
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal

    class Sync:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn):
            future = Future()
            try:
                future.set_result(fn())
            except Exception as exc:  # noqa: BLE001 - the view shows any refresh failure
                future.set_exception(exc)
            return future

    def refresh():
        raise RuntimeError("tracker unreachable")

    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    clock = iter([0] + [100] * 50)
    frames: list[dict[int, str]] = [{}]
    screen = Mock()
    screen.getmaxyx.return_value = (height, 100)
    screen.getch.side_effect = [-1, ord("q")]
    # The screen is erased once; each refresh ends one painted frame.
    screen.refresh.side_effect = lambda: frames.append({})
    screen.addstr.side_effect = lambda y, x, value, attr: frames[-1].__setitem__(y, frames[-1].get(y, "") + value)
    monkeypatch.setattr(watch.sys, "stdout", StringIO())
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(watch, "ThreadPoolExecutor", Sync)
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    real = watch._frame
    monkeypatch.setattr(watch, "_frame", lambda *a, state, **k: real(
        *a, state=state.setdefault("unfolded", set()).update(unfolded) or state, **k))
    watch_terminal._terminal(screen, refresh, snapshot, config, tmp_path, lambda: snapshot.captured_at,
                             StateStore(tmp_path))
    first, second = frames[:2]
    assert screen.erase.call_count == 1
    assert "tracker unreachable" not in "".join(first.values())
    assert "tracker unreachable" in second[height - 3]
    assert sum("tracker unreachable" in text for text in second.values()) == 1
    assert "q " in second[height - 1] and "Claude" in second[height - 2], "usage and hints stay last"
    if unfolded:
        assert "Option" in first[height - 3] and "Option" not in "".join(second.values())
    else:
        # F fits: the usage row stays above the hints; the error takes the last body row.
        assert "Claude —" in first[height - 2] and "Claude —" in second[height - 2]


def test_row_identity_decides_fold_and_box_lane_when_an_issue_shows_twice(tmp_path):
    """Class: per-row data keyed by row position: Enter and clicks act on that row's acorn."""
    snapshot, config = lane_case()
    store = StateStore(tmp_path)
    state = {}
    whole(snapshot, config, tmp_path, state)
    marked = dict(state["rows"])
    current_row = next(y for y, number in marked.items() if number == 3)
    queue_row = next(y for y, number in marked.items() if number == 4)
    assert queue_row > current_row and state["row_lanes"] == {current_row: "work", queue_row: "work"}
    # Keys: the highlighted row decides which acorn Enter explains.
    state["scroll"] = queue_row
    watch.handle_key("enter", store, config, state=state)
    assert state["explanation_target"] == ("acorn", 4)
    watch.handle_key("x", store, config, state=state)
    state["scroll"] = current_row
    watch.handle_key("enter", store, config, state=state)
    assert state["explanation_target"] == ("acorn", 3)
    # Clicks after a scrolled height render: screen rows map back to the right F rows.
    for row, number in ((current_row, 3), (queue_row, 4)):
        # The scroll counts body rows: the one pinned header row is not one of them.
        state, layout = {"scroll": row - 1}, {}
        watch.render_frame(snapshot, config, tmp_path, width=100, height=8, state=state, layout=layout)
        screen = {frame_row: y for y, frame_row in layout["row_map"].items()}
        assert state["scroll"] > 0 and row in screen, (state["scroll"], row)
        x0, y0, *_ = next(hit for hit in layout["hits"] if hit[1] == screen[row] and hit[4] == ("acorn", number))
        watch.handle_mouse("left", x0, y0, layout, store, config, state=state)
        assert state["explanation_target"] == ("acorn", number) and state["scroll"] > 0, row


def test_a_stateless_height_render_keeps_its_pins_and_counts_only_body_rows(tmp_path, monkeypatch):
    """Class: pinned metadata lost when the caller owns no state (render_frame without state)."""
    from types import SimpleNamespace

    from foragent import watch_layout

    def section(name, order, lines, pin=None):
        return SimpleNamespace(__name__="foragent.watch_sections." + name, ORDER=order, PIN=pin, GAP=False,
                               GAP_AFTER=False, render=lambda *args, **context: [[(line, "")] for line in lines])

    monkeypatch.setattr(watch_layout, "_sections", lambda: [
        section("head", 1, ["HEADER"], "header"),
        section("body", 2, [f"BODY {index}" for index in range(20)]),
        section("foot", 3, ["USAGE", "HINTS"], "footer")])
    snapshot, config = lane_case()
    lines = watch.render_frame(snapshot, config, tmp_path, width=80, height=5).splitlines()
    assert lines[0] == "HEADER" and lines[-2:] == ["USAGE", "HINTS"]
    assert lines[1] == "BODY 0" and lines[2].startswith("1–1/20줄"), lines


def test_a_refresh_error_over_the_only_body_row_leaves_nothing_to_open(tmp_path, monkeypatch):
    """Class: a screen-row map edited for painting but not in the state the keys read."""
    import curses
    from concurrent.futures import Future
    from io import StringIO
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal

    class Sync:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn):
            future = Future()
            try:
                future.set_result(fn())
            except Exception as exc:  # noqa: BLE001 - the view shows any refresh failure
                future.set_exception(exc)
            return future

    def refresh():
        raise RuntimeError("tracker unreachable")

    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    clock = iter([0] + [100] * 50)
    screen = Mock()
    screen.getmaxyx.return_value = (4, 100)
    screen.getch.side_effect = [-1, 10, ord("q")]
    seen = []
    real_key = watch.handle_key

    def handle_key(key, *args, state=None, **kwargs):
        if key == "enter":
            seen.append((watch.highlighted_row(state), dict(state.get("row_map") or {})))
        return real_key(key, *args, state=state, **kwargs)

    monkeypatch.setattr(watch, "handle_key", handle_key)
    monkeypatch.setattr(watch.sys, "stdout", StringIO())
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(watch, "ThreadPoolExecutor", Sync)
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    watch_terminal._terminal(screen, refresh, snapshot, config, tmp_path, lambda: snapshot.captured_at,
                             StateStore(tmp_path))
    painted = "".join(call.args[2] for call in screen.addstr.call_args_list)
    assert "tracker unreachable" in painted
    (highlight, row_map), = seen
    assert 1 not in row_map, "the row under the error is not on screen for the keys"
    assert highlight is None, "Enter has no visible issue row to open"


def test_the_top_line_counts_only_running_work_and_only_real_questions(tmp_path):
    """Class: pending or held work read as running (or as a question) in the top line."""
    def top(snapshot):
        return watch.render_frame(snapshot, config, tmp_path, width=120).splitlines()[0]

    snapshot, config = lane_case()
    assert "잘 돌고 있어요" in top(snapshot)
    queued, _ = lane_case(running=False, queued=(4, 5))
    assert "쉬고 있어요" in top(queued), "queued work is pending, not running"
    blocked, _ = lane_case(running=False, blocked=True)
    # A live run on a blocked issue is listed in active_runs, but no worker is running.
    blocked.status["active_runs"] = [{"number": 3, "lane": "work", "attempts": 1}]
    assert "쉬고 있어요" in top(blocked), "blocked work is held, not running"
    parked, _ = lane_case(running=False, queued=())
    parked.status["lanes"]["work"].update(state="waiting-human", issue=3)
    parked.status["summary"] = {**parked.status["summary"], "waiting_human": 1}
    parked.status["issues"] = {"needs-human": []}
    line = top(parked)
    assert "쉬고 있어요" in line and "답할 질문" not in line, "parked work asks no question"
    parked.status["issues"] = {"needs-human": [{"number": 3}, {"number": 3}]}
    assert "답할 질문 1개" in top(parked), "one distinct needs-human issue is one question"

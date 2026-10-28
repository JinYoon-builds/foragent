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
    """One slice rule: F[scroll : scroll + h - 1] plus the position line, N = len(F)."""
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    state = {"unfolded": {"work"}, "scroll": 999}
    full = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, state=state)).splitlines()
    shown = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)).splitlines()
    assert state["scroll"] == len(full) - 11
    assert shown[:-1] == full[-11:], "F's own footer row is an ordinary row and scrolls with it"
    assert full[-1].startswith("j/k 이동")
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


def test_held_issues_beside_a_worker_each_get_a_row(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=())
    row = snapshot.status["lanes"]["work"]
    row["held"] = [{"number": 7, "title": "Blocked one", "state": "blocked"},
                   {"number": 8, "title": "Needs you", "state": "waiting-human"}]
    for state in ({}, {"unfolded": {"work"}}):
        lines = [line for line in plain_frame(whole(snapshot, config, tmp_path, state)).splitlines()
                 if line.startswith(("🐿", "⏳", "✋"))]
        assert [int(re.search(r"#(\d+)", line)[1]) for line in lines] == [3, 7, 8, 4]
        assert lines[1].startswith("⏳") and "오케스트레이터 기다리는 중" in lines[1]
        assert lines[2].startswith("✋") and "사람 기다리는 중" in lines[2]


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
    lines = [line for line in plain_frame(whole(snapshot, config, tmp_path, {})).splitlines()
             if line.startswith("⏳")]
    assert lines and all(line.endswith("○○○○○") for line in lines), lines


def test_a_frame_draws_the_same_rows_with_or_without_view_state(tmp_path):
    """Class: content must not depend on whether a caller passed view state."""
    snapshot, config = lane_case()
    printed = watch.render_frame(snapshot, config, tmp_path, width=100)
    live = watch.render_frame(snapshot, config, tmp_path, width=100, state={})
    assert printed == live
    assert numbers(printed) == [2, 3, 4] and "▸ 3개 더" in printed
    assert len(printed.splitlines()) <= 24


def test_a_finished_orders_idle_lane_still_offers_its_finished_rows(tmp_path):
    """No order-level exception: a folded idle lane with finished rows shows its toggle."""
    snapshot, config = lane_case(running=False, queued=(), done=tuple(range(1, 13)))
    snapshot = replace(snapshot, total=snapshot.completed)
    for state in (None, {}):
        frame = whole(snapshot, config, tmp_path, state)
        assert "▸ 12개 더" in frame and numbers(frame) == []
    unfolded = whole(snapshot, config, tmp_path, {"unfolded": {"work"}})
    assert numbers(unfolded) == list(range(1, 13)) and "▾ 접기" in unfolded


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
    snapshot = replace(snapshot, total=snapshot.completed)
    store, state, layout = StateStore(tmp_path), {}, {}
    folded = "\n".join("".join(value for value, _ in line) for line in watch._frame(
        snapshot, config, tmp_path, tick=0, width=100, selected=None, now=snapshot.captured_at,
        height=None, state=state, layout=layout))
    assert "▸ 2개 더" in folded
    clicks = {target for *_, target in layout["hits"]}
    assert {("acorn", 1), ("acorn", 2)} <= clicks, "each merge the order lists opens its box"
    x0, y0, *_ = next(hit for hit in layout["hits"] if hit[4] == ("acorn", 2))
    watch.handle_mouse("left", x0, y0, layout, store, config, state=state)
    assert state["issue"] == 2
    watch.handle_key("x", store, config, state=state)
    watch.handle_key("a", store, config, state=state)
    opened = whole(snapshot, config, tmp_path, state)
    assert numbers(opened) == [1, 2] and "▾ 접기" in opened
    watch.handle_key("a", store, config, state=state)
    assert numbers(whole(snapshot, config, tmp_path, state)) == []


def test_missing_times_show_a_dash_on_every_row_kind(tmp_path):
    snapshot, config = lane_case(queued=(4,), done=())
    row = snapshot.status["lanes"]["work"]
    row.update(elapsed_seconds=None)
    row["held"] = [{"number": 7, "title": "Blocked one", "state": "blocked"}]
    lines = [line for line in plain_frame(whole(snapshot, config, tmp_path, {})).splitlines()
             if line.startswith(("🐿", "⏳"))]
    assert len(lines) == 3
    assert all(re.search(r"— +○○○○○$", line) for line in lines), lines


def test_a_stopped_run_keeps_its_phase_in_row_and_box(tmp_path):
    snapshot, config = lane_case(running=False, queued=(), done=())
    row = snapshot.status["lanes"]["work"]
    row["held"] = [{"number": 9, "title": "Stopped", "state": "working"}]
    snapshot = replace(snapshot, run_phases={9: "reviewing"})
    assert "리뷰 받는 중" in [line for line in whole(snapshot, config, tmp_path, {}).splitlines() if "#9" in line][0]

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
    assert watch.render_frame(snapshot, config, tmp_path, width=200).splitlines()[-1] == (
        "1–1: 담당 보기 · j/k 이동 · Enter 열기 · f 접기 · a 모두 · o 오케스트레이터 · x 닫기 · q 끝내기")
    assert "acorns_keys" in acorns.MESSAGES["en"]
    assert all("j/k move" not in value for module in (squirrel, shared)
               for table in module.MESSAGES.values() for value in table.values())


def test_hits_use_screen_rows_after_a_height_render(tmp_path):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    state, whole_layout, layout = {"unfolded": {"work"}}, {}, {}
    watch.render_frame(snapshot, config, tmp_path, width=100, state=state, layout=whole_layout)
    state["scroll"] = 5
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state, layout=layout)
    expected = [(x0, y0 - 5, x1, y1 - 5, target) for x0, y0, x1, y1, target in whole_layout["hits"]
                if 5 <= y0 and y1 - 5 <= 11]
    assert layout["hits"] == expected and expected
    assert all(0 <= y0 < y1 <= 11 for _, y0, _, y1, _ in layout["hits"]), "clipped to the h-1 visible rows"
    x, y = next((x0, y0) for x0, y0, _, _, target in layout["hits"] if target == ("acorn", 6))
    watch.handle_mouse("left", x, y, layout, StateStore(tmp_path), config, state=state)
    assert state["issue"] == 6, "a screen-row click opens the issue shown on that row"


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
    assert len(frame) < 60 and [tall.get(y, "").rstrip() for y in range(len(frame))] == frame, (
        "a tall screen paints F itself: the same 24-row trimming, no extra sections")
    assert not any(text.strip() for y, text in tall.items() if y >= len(frame))


def test_only_the_first_visible_issue_row_is_highlighted(tmp_path, monkeypatch):
    """Class: selection by row position, not issue identity (repeats and wrapped rows)."""
    snapshot, config = lane_case(running=False, queued=(), done=(1, 2))
    snapshot = replace(snapshot, total=snapshot.completed)
    # Unfolded, #1 and #2 show as lane rows and again in the finished order's list.
    rows, reverse = live_paint(tmp_path, monkeypatch, snapshot, config, height=60, unfolded={"work"})
    assert sum("#1 " in text or text.rstrip().endswith("#1") for text in rows.values()) >= 2
    assert len(reverse) == 1 and "#1" in rows[min(reverse)] and rows[min(reverse)].startswith("💤")
    # A long title wraps the list item onto two lines; still one highlighted row.
    issue = replace(snapshot.done["work"][0], title="Done " + "word " * 40)
    wrapped = replace(snapshot, done={"work": [issue, snapshot.done["work"][1]]})
    state = {}
    watch._frame(wrapped, config, tmp_path, tick=0, width=100, selected=None, now=wrapped.captured_at, state=state)
    assert [number for _, number in state["rows"]].count(1) >= 2, "the item wraps onto two issue rows"
    rows, reverse = live_paint(tmp_path, monkeypatch, wrapped, config, height=60, unfolded={"work"})
    assert len(reverse) == 1 and rows[min(reverse)].startswith("💤")


def test_closing_the_box_keeps_the_rows_scroll(tmp_path):
    snapshot, config = lane_case(queued=tuple(range(4, 40)))
    store, state = StateStore(tmp_path), {"unfolded": {"work"}}
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    for _ in range(7):
        watch.handle_key("j", store, config, state=state)
    assert state["scroll"] == 7
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    watch.handle_key("enter", store, config, state=state)
    opened = state["issue"]
    assert opened is not None and state["scroll"] == 0, "the box opens at its top"
    watch.render_frame(snapshot, config, tmp_path, width=100, height=12, state=state)
    for key in ("x", "x"):
        watch.handle_key(key, store, config, state=state)
        assert state["issue"] is None and state["scroll"] == 7, "x closes once, then does nothing"
    watch.handle_mouse("left", 0, 0, {"hits": [(0, 0, 9, 1, ("acorn", 9))]}, store, config, state=state)
    watch.handle_mouse("left", 0, 0, {"hits": [(0, 0, 9, 1, ("acorn", 9))]}, store, config, state=state)
    assert state["issue"] is None and state["scroll"] == 7, "a click that opens then closes keeps it too"


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
    frame = watch.render_frame(lane_case()[0], config, tmp_path, width=100, state=state)
    assert "│  printf 'a  b'" in frame and "│      kept  indent" in frame and "│      indented  first line" in frame


def test_a_wrapped_box_line_joins_back_to_the_exact_text(tmp_path):
    from foragent.watch_sections.acorns import _verbatim_wrap

    line = "  run --flag  'two  spaces'  다람쥐도토리 " + "x" * 30
    for width in (2, 7, 12, 20):
        pieces = _verbatim_wrap(line, width)
        assert "".join(pieces) == line
        assert all(watch._cells(piece) <= width for piece in pieces)
    store, config = box_case(tmp_path, "## Done when\n```sh\n" + line + "\n```\n")
    frame = watch.render_frame(lane_case()[0], config, tmp_path, width=24, state={"issue": 5, "store": store})
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

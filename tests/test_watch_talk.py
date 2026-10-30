"""Talking to a lane's worker from the view: squirrel clicks, the message line and status facts (#174)."""

from dataclasses import replace
from datetime import timedelta
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_squirrel_running import plain_frame  # noqa: E402
from test_watch_acorns import lane_case  # noqa: E402

from foragent import lanes, status as status_module, watch  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.watch_layout import SCREEN_ROWS  # noqa: E402


@pytest.fixture(autouse=True)
def emoji_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)


@pytest.fixture
def talks(monkeypatch):
    calls = []

    def talk(number, **kwargs):
        calls.append((number, kwargs))
        return f"Opened #{number}"

    monkeypatch.setattr(lanes, "talk", talk)
    return calls


def live(snapshot, config, root, state, width=80):
    layout = {}
    frame = watch.render_frame(snapshot, config, root, width=width, state=state, layout=layout)
    return plain_frame(frame), layout


def squirrel(layout, slug="work"):
    return next((x0, y0) for x0, y0, _, _, target in layout["hits"] if target == ("squirrel", slug))


def test_live_squirrel_click_talks_and_the_line_stays_until_a_key_or_ten_seconds(tmp_path, monkeypatch, talks):
    snapshot, config = lane_case()
    store, state = StateStore(tmp_path / "state"), {}
    clock = [100.0]
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock[0])
    _, layout = live(snapshot, config, tmp_path, state)
    x, y = squirrel(layout)
    watch.handle_mouse("left", x, y, layout, store, config, state=state)
    assert [(number, kwargs["store"], kwargs["repo_root"]) for number, kwargs in talks] == [(3, store, tmp_path)]
    assert talks[0][1]["message"] is None
    assert "Opened #3" in live(snapshot, config, tmp_path, state)[0]
    clock[0] += 10
    assert "Opened #3" in live(snapshot, config, tmp_path, state)[0]
    clock[0] += 1
    assert "Opened #3" not in live(snapshot, config, tmp_path, state)[0]


def test_a_lane_error_is_shown_instead_of_raised(tmp_path, monkeypatch):
    def refuse(number, **kwargs):
        raise lanes.LaneError(f"#{number} has no session")

    monkeypatch.setattr(lanes, "talk", refuse)
    snapshot, config = lane_case()
    store, state = StateStore(tmp_path / "state"), {}
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    assert "#3 has no session" in live(snapshot, config, tmp_path, state)[0]


def test_idle_lane_talks_to_its_last_issue_and_a_lane_without_one_does_nothing(tmp_path, talks):
    snapshot, config = lane_case(running=False, queued=(), done=())
    store = StateStore(tmp_path / "state")
    state = {}
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    assert talks == [] and "talk_message" not in state
    lanes_ = {"work": {**snapshot.status["lanes"]["work"], "last_issue": 9}}
    snapshot = replace(snapshot, status={**snapshot.status, "lanes": lanes_})
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    assert [number for number, _ in talks] == [9]


def test_squirrel_owns_only_the_glyph_cell_and_the_rest_of_the_row_opens_the_box(tmp_path):
    snapshot, config = lane_case()
    frame, layout = live(snapshot, config, tmp_path, {})
    y = next(index for index, line in enumerate(frame.splitlines()) if "#3" in line)
    assert frame.splitlines()[y].startswith("🐿")
    assert watch.hit_test(layout, 0, y) == watch.hit_test(layout, 1, y) == ("squirrel", "work")
    assert watch.hit_test(layout, 2, y) == ("acorn", 3)


def test_talking_badge_above_the_lane_or_words_in_text_mode(tmp_path):
    snapshot, config = lane_case(queued=())
    runs = [{**snapshot.status["active_runs"][0], "talking": True}]
    talking = replace(snapshot, status={**snapshot.status, "active_runs": runs})
    frame, layout = live(talking, config, tmp_path, None, width=60)
    lines = frame.splitlines()
    first = min(y0 for _, y0, _, _, target in layout["hits"] if target == ("squirrel", "work"))
    assert lines[first - 1] == "💬" and frame.count("💬") == 1
    assert "대화 중 💬" not in frame
    narrow = live(talking, config, tmp_path, None, width=40)[0]
    assert "대화 중 💬" in narrow and narrow.count("💬") == 1
    assert "💬" not in live(snapshot, config, tmp_path, None, width=60)[0]
    assert "💬" not in live(snapshot, config, tmp_path, None, width=40)[0]


def test_status_exposes_talking_and_the_last_finished_issue_by_finish_time(tmp_path, monkeypatch):
    snapshot, config = lane_case()
    clock = [snapshot.captured_at]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(9, branch="b9", worktree=str(tmp_path / "9"), lane="work")
    store.start_run(4, branch="b4", worktree=str(tmp_path / "4"), lane="work")
    store.finish_run(9, "merged")
    clock[0] += timedelta(minutes=5)
    store.finish_run(4, "failed")  # The newest finish of any outcome wins, not the larger number.
    clock[0] += timedelta(minutes=5)
    store.start_run(5, branch="b5", worktree=str(tmp_path / "5"), lane="work")
    monkeypatch.setattr(type(store.get_run(5)), "talking", property(lambda run: run.number == 5))
    rows, _ = status_module.collect_lanes([], {}, {}, store, config, clock[0])
    assert rows["work"]["last_issue"] == 4
    assert rows["work"]["issue"] == 5 and rows["work"]["talking"] is True
    assert rows["main"]["last_issue"] is None and rows["main"]["talking"] is False


def test_long_expanded_lane_rows_never_trim_the_talk_line_away(tmp_path, monkeypatch, talks):
    snapshot, config = lane_case(queued=tuple(range(10, 41)))
    store, state = StateStore(tmp_path / "state"), {"unfolded": {"work"}}
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    from foragent.watch_mouse import talk_alert

    lines = watch._frame(snapshot, config, tmp_path, tick=0, width=80, selected=None,
                         now=snapshot.captured_at, state=state)
    plain = ["".join(value for value, _ in line).rstrip() for line in lines]
    assert len(plain) > SCREEN_ROWS
    # Kept with the lane rows, before any key or timeout, directly under the lane rows (the fold line).
    assert plain.count("Opened #3") == 1
    assert plain[plain.index("Opened #3") - 1].strip() == "▾ 접기"
    # A screen showing only the first rows carries it in its bottom message row.
    assert talk_alert(state, lines[:SCREEN_ROWS - 1], 80) == [watch._fit(watch._line("Opened #3", "yellow"), 80)]


def test_live_bottom_row_carries_the_talk_line_only_when_the_screen_does_not_show_it():
    from foragent.watch_mouse import show_talk, talk_alert

    state = {}
    show_talk(state, "Opened #3")
    shown = watch._fit(watch._line("Opened #3", "yellow"), 40)
    assert talk_alert(state, [watch._line("rows"), shown], 40) == []
    assert talk_alert(state, [watch._line("rows")], 40) == [shown]
    assert talk_alert({}, [watch._line("rows")], 40) == []


def fitted_case(language="ko", workers=1, *, lane="abcdefghijklmnop", number=123456):
    """A lane with a long name whose first worker (a long issue number) is talking."""
    snapshot, config = lane_case(queued=())
    numbers = [number, 8][:workers]
    row = {**snapshot.status["lanes"]["work"], "lane_display": lane, "issue": number,
           "running": [{"number": n, "title": f"Task {n}"} for n in numbers]}
    runs = [{"number": n, "lane": "work", "attempts": 1, "started_at": f"2026-10-24T08:0{index}:00+00:00",
             "talking": n == number} for index, n in enumerate(numbers)]
    status = {**snapshot.status, "lanes": {"work": row}, "active_runs": runs}
    return replace(snapshot, status=status), replace(config, language=language)


@pytest.mark.parametrize("language,said", [("ko", "대화 중 💬"), ("en", "talking 💬")])
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("glyphs", ["emoji", "ascii"])
def test_talk_indicator_is_reserved_before_names_and_phase_words_shrink(tmp_path, language, said, workers, glyphs):
    snapshot, config = fitted_case(language, workers)
    config = replace(config, view_glyphs=glyphs)
    for width in range(20, 121):
        frame = live(snapshot, config, tmp_path, None, width=width)[0]
        lines = frame.splitlines()
        row = next(index for index, line in enumerate(lines) if "#123456" in line)
        other = [line for line in lines if "#8" in line]
        if width < 44:
            # Text mode: the whole 💬 stays in the row; the words stay while the row has room.
            assert "💬" in lines[row] and frame.count("💬") == 1, (width, frame)
            assert all("💬" not in line for line in other)
            if width >= 40:
                assert said in lines[row], (width, lines[row])
        else:
            assert frame.count("💬") == 1 and said not in frame, (width, frame)
            first = next(index for index, line in enumerate(lines) if line.startswith(("🐿", "💤", "[*]", "[z]")))
            assert lines[first - 1] == "💬", (width, frame)
        assert (len(other) == 1) == (workers == 2)


def test_animation_off_is_text_mode_at_any_width(tmp_path):
    snapshot, config = fitted_case()
    frame = live(snapshot, replace(config, animation=False), tmp_path, None, width=120)[0]
    assert "대화 중 💬" in next(line for line in frame.splitlines() if "#123456" in line)
    assert frame.count("💬") == 1


def test_windowed_screen_reserves_a_row_for_the_talk_line_at_every_height(tmp_path, talks):
    snapshot, config = lane_case(queued=tuple(range(10, 41)))
    store, state = StateStore(tmp_path / "state"), {"unfolded": {"work"}}
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    for height in range(3, 30):
        for scroll in (0, 1000):
            state["scroll"] = scroll
            layout = {}
            lines = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=80, height=height,
                                                   state=state, layout=layout)).splitlines()
            assert lines.count("Opened #3") == 1, (height, scroll, lines)
            assert len(lines) <= height
            talk_row = lines.index("Opened #3")
            assert not any(y0 <= talk_row < y1 for _, y0, _, y1, _ in layout["hits"])
            highlighted = watch.highlighted_row(state)
            assert highlighted is None or highlighted - state["scroll"] != talk_row


def painted(snapshot, config, root, state, height, error="", width=80):
    """What the live view paints: _frame, its window, then the bottom rows, cut to the screen."""
    from foragent.watch_mouse import screen_rows

    frame = watch._frame(snapshot, config, root, tick=0, width=width, selected=None,
                         now=snapshot.captured_at, state=state)
    visible = watch._window(frame, state, height, width, config.language, {})
    rows = screen_rows(visible, state, len(frame), height, width, error)[:max(1, height)]
    return ["".join(value for value, _ in line).rstrip() for line in rows]


def clicked_long_lane(tmp_path, talks):
    snapshot, config = lane_case(queued=tuple(range(10, 41)))
    store, state = StateStore(tmp_path / "state"), {"unfolded": {"work"}}
    _, layout = live(snapshot, config, tmp_path, state)
    watch.handle_mouse("left", *squirrel(layout), layout, store, config, state=state)
    return snapshot, config, state


def test_live_screen_paints_the_talk_line_at_every_height_from_one_row(tmp_path, talks):
    snapshot, config, state = clicked_long_lane(tmp_path, talks)
    for height in range(1, 30):
        for scroll in (0, 1000):
            state["scroll"] = scroll
            rows = painted(snapshot, config, tmp_path, state, height)
            assert len(rows) <= height and rows.count("Opened #3") == 1, (height, scroll, rows)


def test_live_screen_keeps_both_the_talk_line_and_a_refresh_error(tmp_path, talks):
    snapshot, config, state = clicked_long_lane(tmp_path, talks)
    for height in range(2, 30):
        rows = painted(snapshot, config, tmp_path, state, height, error="refresh failed")
        assert rows.count("Opened #3") == 1 and rows.count("refresh failed") == 1, (height, rows)
    # One row: both share it, the talk result first.
    assert painted(snapshot, config, tmp_path, state, 1, error="refresh failed") == [
        "Opened #3 · refresh failed"]


def test_screen_rows_without_a_talk_line_keep_todays_bottom_row():
    from foragent.watch_mouse import screen_rows

    frame = [watch._line(f"row {index}") for index in range(4)]
    text = lambda rows: ["".join(value for value, _ in line).rstrip() for line in rows]
    assert text(screen_rows(frame, {}, 4, 6, 40)) == ["row 0", "row 1", "row 2", "", "", "row 3"]
    assert text(screen_rows(frame, {}, 4, 6, 40, "failed")) == ["row 0", "row 1", "row 2", "", "", "failed"]
    assert text(screen_rows(frame, {}, 10, 4, 40, "failed")) == ["row 0", "row 1", "row 2", "failed"]
    assert text(screen_rows(frame, {}, 10, 4, 40)) == ["row 0", "row 1", "row 2", "row 3"]

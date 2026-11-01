"""The finished scene: a short fireworks band, then a calm frame (#168)."""

from dataclasses import replace

import pytest
import yaml

from conftest import BASE_TIME
from foragent import watch, watch_layout
from foragent.config import Config, ConfigError, load_config
from foragent.tracker import FakeTracker
from foragent.watch_sections import finished


def snapshot_for(order=99, *, done=True, requests=(), lanes=1):
    slugs = ["main", *(f"lane{index}" for index in range(1, lanes))]
    config = Config(language="ko", skill_libraries=[], lanes={
        slug: {"title": "담당" if slug == "main" else f"담당{slug[4:]}", "emoji": "", "paths": [f"{slug}/**"]}
        for slug in slugs})
    tracker = FakeTracker()
    tracker.add_issue(order, "Order")
    tracker.add_issue(1, "Finished work", labels=["lane:main"], state="closed" if done else "open")
    rows = {slug: {"slug": slug, "lane_display": config.lanes[slug]["title"], "state": "idle", "issue": None,
                   "title": None, "phase": None, "elapsed_seconds": None, "last_activity_seconds": None,
                   "waiting_for": [], "running": [], "ready": [], "queued": []} for slug in slugs}
    status = {"lanes": rows, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [], "history": []}
    done_issues = {"main": [tracker.get_issue(1)] if done else []}
    snapshot = watch.Snapshot(status, done_issues, tracker.get_issue(order), 1, 1 if done else 0, None, [],
                              BASE_TIME, requests=list(requests))
    return snapshot, config


def fireworks(frame):
    return any(set(line) <= set(" *+") and ("*" in line or "+" in line) for line in frame.splitlines())


def frame(snapshot, config, tick, **kwargs):
    return watch.render_frame(snapshot, config, ".", tick=tick, width=100, **kwargs)


def test_fireworks_seconds_is_reexported():
    assert watch.FIREWORKS_SECONDS == finished.FIREWORKS_SECONDS == 3


def test_live_state_counts_from_the_first_finished_tick_once_per_order():
    snapshot, config = snapshot_for()
    state = {}
    assert fireworks(frame(snapshot, config, 50, state=state)), "a view opened later still plays them"
    assert fireworks(frame(snapshot, config, 59, state=state))
    assert not fireworks(frame(snapshot, config, 60, state=state)), "3 seconds is 10 ticks"
    assert not fireworks(frame(snapshot, config, 200, state=state))
    other, _ = snapshot_for(order=100)
    assert fireworks(frame(other, config, 300, state=state)), "a different order plays them again"
    assert not fireworks(frame(snapshot, config, 300, state=state))


def test_unfinished_order_does_not_start_the_timer():
    running, config = snapshot_for(done=False)
    state = {}
    assert not fireworks(frame(running, config, 5, state=state))
    assert state.get("fireworks", {}) == {}
    done, _ = snapshot_for()
    assert fireworks(frame(done, config, 40, state=state))


def test_calm_state_and_animation_off_never_play_them():
    snapshot, config = snapshot_for()
    assert fireworks(frame(snapshot, config, 0))
    with finished.calm():
        assert not fireworks(frame(snapshot, config, 0, state={}))
    assert fireworks(frame(snapshot, config, 0)), "calm ends with its block"
    assert not fireworks(frame(snapshot, replace(config, animation=False), 0))


def test_band_sits_directly_above_the_completion_sentence():
    snapshot, config = snapshot_for()
    lines = frame(snapshot, config, 0).splitlines()
    heading = next(index for index, line in enumerate(lines) if "이번 주문을 모두 끝냈어요" in line)
    assert all(fireworks(line) for line in lines[heading - 3:heading])
    assert not fireworks(lines[heading - 4])


@pytest.mark.parametrize("width, height", [(39, 24), (100, 15)])
def test_band_drops_on_narrow_or_short_terminals(width, height):
    snapshot, config = snapshot_for()
    lines = watch._frame(snapshot, config, ".", tick=0, width=width, selected=None, now=BASE_TIME,
                         height=height)
    text = "\n".join("".join(value for value, _ in line) for line in lines)
    assert not fireworks(text)
    assert "담당" in text, "the lane rows stay"


def text_of(lines):
    return "\n".join("".join(value for value, _ in line) for line in lines)


def test_short_screen_keeps_the_playing_band_and_drops_other_sections_first():
    snapshot, config = snapshot_for()
    full = watch._frame(snapshot, config, ".", tick=0, width=100, selected=None, now=BASE_TIME, height=None)
    dropped_list = False
    for height in range(len(full), finished.MIN_HEIGHT - 1, -1):
        text = text_of(watch._frame(snapshot, config, ".", tick=0, width=100, selected=None,
                                    now=BASE_TIME, height=height))
        assert fireworks(text), f"the playing band gave way at {height} rows"
        dropped_list |= "이번 주문을 모두 끝냈어요" not in text
    # Shell: an explicit height no longer drops sections; the viewport scrolls instead.
    assert not dropped_list, "no short screen drops the done list; every row stays reachable"


def test_band_colours_are_the_existing_style_keys_and_change_every_tick():
    rows = [finished.band(tick, 80) for tick in range(4)]
    styles = {style for lines in rows for line in lines for value, style in line if value != " "}
    assert styles == {"yellow", "claude", "divider"}
    assert all(rows[tick] != rows[tick + 1] for tick in range(3))
    assert all(len(lines) == 3 and all(len(line) == 80 for line in lines) for lines in rows)


def test_calm_frame_keeps_the_layout_of_a_view_without_the_band(monkeypatch):
    snapshot, config = snapshot_for()
    state = {}
    watch._frame(snapshot, config, ".", tick=0, width=160, selected=None, now=BASE_TIME, state=state)
    calm = watch._frame(snapshot, config, ".", tick=20, width=160, selected=None, now=BASE_TIME, state=state)
    sections = watch_layout._sections
    monkeypatch.setattr(watch_layout, "_sections",
                        lambda: [section for section in sections() if section is not finished])
    assert calm == watch._frame(snapshot, config, ".", tick=20, width=160, selected=None, now=BASE_TIME)


def test_once_prints_the_calm_frame(tmp_path, monkeypatch, capsys):
    snapshot, config = snapshot_for()
    monkeypatch.setattr(watch, "collect", lambda *args: snapshot)
    watch.run(lambda: {}, FakeTracker(), None, config, tmp_path, once=True)
    out = capsys.readouterr().out
    assert "이번 주문을 모두 끝냈어요" in out and not fireworks(out)


def test_animation_off_hides_the_big_squirrel_but_keeps_the_header_line():
    snapshot, config = snapshot_for()
    off = replace(config, animation=False)
    # Shell's top line (lane_header) replaces the big-squirrel header; scene brings the sprite.
    on = frame(snapshot, config, 0)
    plain = frame(snapshot, off, 0)
    assert "▀" not in plain and "▄" not in plain
    assert plain.splitlines()[0].strip(), "the header keeps its one-line form"
    assert plain.splitlines()[0] == on.splitlines()[0]


def test_animation_off_keeps_the_stage_request_as_one_line():
    snapshot, config = snapshot_for(requests=[{"id": 1, "status": "processing", "text": "Plan the next order"}])
    on = frame(snapshot, config, 0)
    off = frame(snapshot, replace(config, animation=False), 0)
    assert "▀" in on and "▀" not in off
    assert "Plan the next order" in off


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_animation_must_be_a_boolean(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"animation": value}), encoding="utf-8")
    with pytest.raises(ConfigError, match="animation"):
        load_config(tmp_path)


def test_animation_false_loads_from_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".foragent.yml").write_text("animation: false\n", encoding="utf-8")
    assert load_config(tmp_path).animation is False


def test_band_moves_into_the_first_screen_when_lane_rows_push_it_below():
    snapshot, config = snapshot_for(lanes=8)
    lines = watch._frame(snapshot, config, ".", tick=0, width=100, selected=None, now=BASE_TIME, height=16)
    rows = ["".join(value for value, _ in line) for line in lines]
    assert len(rows) > 16, "the KEEP rows alone overflow this screen"
    assert fireworks("\n".join(rows[:15])), "the band is inside the rows the live view shows first"
    assert sum(fireworks(row) for row in rows) == 3, "the band moved, it was not copied"
    names = ["담당", *(f"담당{index}" for index in range(1, 8))]
    lane_rows = [row.split()[1] for row in rows if row.startswith("💤 ")]
    assert lane_rows == names, "every lane row stays, in order"


def test_moved_band_shifts_the_click_areas_of_the_rows_it_passes(monkeypatch):
    from types import SimpleNamespace

    def section(name, lines, **attrs):
        def render(*args, top=0, hits=None, **context):
            for y, line in enumerate(lines):
                if line.startswith("row"):
                    hits.append((0, y, 5, y + 1, line))
            return [[(line, "")] for line in lines]
        return SimpleNamespace(__name__=f"foragent.watch_sections.{name}", ORDER=0, render=render, **attrs)

    tall = section("tall", [f"row{index}" for index in range(14)], KEEP=True, GAP_AFTER=False)
    band = section("band", ["*", "+", "*"], STAYS=True, GAP=False, GAP_AFTER=False)
    foot = section("help", ["footer"], KEEP=True, GAP=False)
    monkeypatch.setattr(watch_layout, "_sections", lambda: [tall, band, foot])
    snapshot, config = snapshot_for()
    layout = {}
    lines = watch._frame(snapshot, config, ".", tick=0, width=20, selected=None, now=BASE_TIME,
                         layout=layout, height=10)
    rows = ["".join(value for value, _ in line).rstrip() for line in lines]
    # Shell: a STAYS band is no longer moved; F keeps its order and the viewport scrolls.
    assert rows[14:17] == ["*", "+", "*"], "the band stays where it is drawn"
    assert rows[:14] + rows[17:] == [*(f"row{index}" for index in range(14)), "footer"]
    assert sorted((y0, item) for x0, y0, x1, y1, item in layout["hits"]) == sorted(
        (rows.index(f"row{index}"), f"row{index}") for index in range(14))


def test_live_terminal_paints_the_band_on_a_short_screen_with_many_lanes(tmp_path, monkeypatch):
    import curses
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal
    from foragent.state import StateStore

    snapshot, config = snapshot_for(lanes=8)
    screen = Mock()
    # F keeps the band within its fixed 24 rows (#170); a 24-row terminal shows it at once.
    screen.getmaxyx.return_value = (24, 100)
    screen.getch.return_value = ord("q")
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path, lambda: BASE_TIME,
                             StateStore(tmp_path))
    painted = [call.args for call in screen.addstr.call_args_list]
    assert all(y < 24 for y, *_ in painted)
    assert {value for y, x, value, *_ in painted} & {"*", "+"}, "fireworks are painted on the screen"

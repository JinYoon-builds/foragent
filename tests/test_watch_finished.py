"""The finished scene: a short fireworks band, then a calm frame (#168)."""

from dataclasses import replace

import pytest
import yaml

from conftest import BASE_TIME
from foragent import watch
from foragent.config import Config, ConfigError, load_config
from foragent.tracker import FakeTracker
from foragent.watch_sections import finished


def snapshot_for(order=99, *, done=True, requests=()):
    lanes = {"main": {"title": "담당", "emoji": "", "paths": ["src/**"]}}
    config = Config(language="ko", lanes=lanes, skill_libraries=[])
    tracker = FakeTracker()
    tracker.add_issue(order, "Order")
    tracker.add_issue(1, "Finished work", labels=["lane:main"], state="closed" if done else "open")
    row = {"slug": "main", "lane_display": "담당", "state": "idle", "issue": None, "title": None,
           "phase": None, "elapsed_seconds": None, "last_activity_seconds": None,
           "waiting_for": [], "running": [], "ready": [], "queued": []}
    status = {"lanes": {"main": row}, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
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


def test_short_screen_drops_the_band_before_the_done_list():
    snapshot, config = snapshot_for()
    full = watch._frame(snapshot, config, ".", tick=0, width=100, selected=None, now=BASE_TIME, height=None)
    seen_list_without_band = False
    for height in range(len(full), finished.MIN_HEIGHT - 1, -1):
        lines = watch._frame(snapshot, config, ".", tick=0, width=100, selected=None, now=BASE_TIME,
                             height=height)
        text = "\n".join("".join(value for value, _ in line) for line in lines)
        listed = "이번 주문을 모두 끝냈어요" in text
        assert listed or not fireworks(text), f"the band outlived the done list at {height} rows"
        seen_list_without_band |= listed and not fireworks(text)
    assert seen_list_without_band, "some short screen keeps the list and drops the band"


def test_once_prints_the_calm_frame(tmp_path, monkeypatch, capsys):
    snapshot, config = snapshot_for()
    monkeypatch.setattr(watch, "collect", lambda *args: snapshot)
    watch.run(lambda: {}, FakeTracker(), None, config, tmp_path, once=True)
    out = capsys.readouterr().out
    assert "이번 주문을 모두 끝냈어요" in out and not fireworks(out)


def test_animation_off_hides_the_big_squirrel_but_keeps_the_header_line():
    snapshot, config = snapshot_for()
    off = replace(config, animation=False)
    assert "▀" in frame(snapshot, config, 0)
    plain = frame(snapshot, off, 0)
    assert "▀" not in plain and "▄" not in plain
    assert plain.splitlines()[0].strip(), "the header keeps its one-line form"


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

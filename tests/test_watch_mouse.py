from datetime import UTC, datetime
import subprocess

import pytest

from foragent import orch_inbox, watch
from foragent.config import Config
from foragent.state import StateStore


NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def status():
    return {"lanes": {}, "active_runs": [], "history": [],
            "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0}}


def snapshot(requests):
    return watch.Snapshot(status(), {}, None, 0, 0, None, [], NOW, requests)


def test_hit_test_prefers_the_last_box_and_misses_outside():
    layout = {"hits": [(0, 0, 10, 5, "under"), (2, 2, 4, 4, "over")]}
    assert watch.hit_test(layout, 3, 3) == "over"
    assert watch.hit_test(layout, 0, 0) == "under"
    assert watch.hit_test(layout, 10, 0) is None
    assert watch.hit_test({}, 0, 0) is None


def test_click_ignores_tmux_notes_right_clicks_and_missing_orca(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    monkeypatch.setattr(watch.shutil, "which", lambda name: "/bin/orca")
    layout = {"hits": [(0, 0, 5, 5, ("orchestrator", None))]}
    orch_inbox.write_note(store, "지금", None, None, {"kind": "tmux", "pane": "%1"})
    watch.handle_mouse("left", 1, 1, layout, store, Config())
    orch_inbox.write_note(store, "지금", None, None, {"kind": "orca", "handle": "t1", "title": None})
    watch.handle_mouse("right", 1, 1, layout, store, Config())
    watch.handle_mouse("left", 9, 9, layout, store, Config())
    assert calls == []
    watch.handle_mouse("left", 1, 1, layout, store, Config())
    assert calls == [["/bin/orca", "terminal", "switch", "--terminal", "t1"]]
    monkeypatch.setattr(watch.shutil, "which", lambda name: None)
    watch.handle_mouse("left", 1, 1, layout, store, Config())
    assert len(calls) == 1


def test_failed_switch_does_not_raise(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    orch_inbox.write_note(store, "지금", None, None, {"kind": "orca", "handle": "gone", "title": None})
    monkeypatch.setattr(watch.shutil, "which", lambda name: "/bin/orca")

    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", fail)
    assert watch.switch_to_orchestrator(store) is False


@pytest.mark.parametrize("key", ["x", "esc"])
def test_keys_close_a_caller_supplied_legacy_box_and_restore_its_scroll(key):
    state = {"issue": 7, "scroll": 1, "rows_scroll": 4}
    assert watch.handle_key(key, None, Config(), state=state) is False
    assert state == {"issue": None, "scroll": 4}


def test_note_location_prefers_orca_then_tmux_then_terminal():
    assert orch_inbox.note_location({"ORCA_TERMINAL_HANDLE": "h", "TMUX": "x", "TMUX_PANE": "%2"}, "orch: p") == {
        "kind": "orca", "handle": "h", "title": "orch: p"}
    assert orch_inbox.note_location({"TMUX": "x", "TMUX_PANE": "%2"}) == {"kind": "tmux", "pane": "%2"}
    assert orch_inbox.note_location({}, tmux_pane="%7") == {"kind": "tmux", "pane": "%7"}
    assert orch_inbox.note_location({"TMUX_PANE": "%2"}) == {"kind": "terminal"}


def test_request_line_and_bubble_in_english_and_width_tiers(tmp_path):
    config = Config(language="en")
    rows = [{"status": "processing", "text": "D75 order\nsecond line"}, {"status": "done", "text": "old"},
            {"status": "new", "text": "later"}]
    frame = watch.render_frame(snapshot(rows), config, tmp_path, width=160, now=NOW)
    assert "requests 1 active · 1 done" in frame
    bubble = [line for line in frame.splitlines() if "Planning request: D75 order" in line]
    assert len(bubble) == 1 and "▀" in bubble[0] and "second line" not in frame
    stacked = watch.render_frame(snapshot(rows), config, tmp_path, width=50, now=NOW).splitlines()
    line = next(index for index, value in enumerate(stacked) if "Planning request" in value)
    # Below 80 columns the bubble takes the whole width, without the sprite beside it.
    top = next(index for index, value in enumerate(stacked) if value.startswith("╭"))
    assert top < line < top + 7 and not any("▀" in value or "▄" in value for value in stacked[top:top + 8])
    text = watch.render_frame(snapshot(rows), config, tmp_path, width=40, now=NOW)
    assert "Planning request" in text and "▀" not in text
    idle = watch.render_frame(snapshot([]), config, tmp_path, width=160, now=NOW)
    # Without a request the stage is gone; the idle header's squirrel has no bubble.
    assert "requests" not in idle and "Planning request" not in idle

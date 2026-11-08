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
    rows = [{"id": 1, "from": "planning", "status": "processing", "text": "D75 order\nsecond line"}, {"status": "done", "text": "old"},
            {"status": "new", "text": "later"}]
    layout, state = {}, {}
    frame = watch.render_frame(snapshot(rows), config, tmp_path, width=160, now=NOW, layout=layout, state=state)
    assert "requests 1 active · 1 done" not in frame  # Removed from r53 main.
    # #2824: the default bubble shows the count; clicking it lists open request subjects.
    count = "1 planning-session request ▸ click for the list"
    bubble = [line for line in frame.splitlines() if count in line]
    assert len(bubble) == 1 and "▀" in bubble[0]
    assert "Planning request:" not in frame and "D75 order" not in frame and "second line" not in frame
    request_hits = [hit for hit in layout["hits"] if hit[4] == ("requests", None)]
    assert len(request_hits) == 1
    x, y, *_ = request_hits[0]
    assert watch.hit_test(layout, x, y) == ("requests", None)
    watch.handle_mouse("left", x, y, layout, StateStore(tmp_path), config, state=state)
    assert state["explanation_target"] == ("requests", None)
    selected = watch.render_frame(snapshot(rows), config, tmp_path, width=160, now=NOW, state=state)
    body = [line[23:-1].strip() for line in selected.splitlines()
            if len(line) > 21 and line[21] in ("│", "┤")]
    assert body == ["1 open planning-session request:", "#1 D75 order", "", "", "", ""]
    stacked = watch.render_frame(snapshot(rows), config, tmp_path, width=50, now=NOW).splitlines()
    line = next(index for index, value in enumerate(stacked) if "1 planning-session request" in value)
    # Below 80 columns the bubble takes the whole width, without the sprite beside it.
    top = next(index for index, value in enumerate(stacked) if value.startswith("╭"))
    assert [value[2:-2].rstrip() for value in stacked[top + 1:top + 3]] == [
        "1 planning-session request ▸ click for the", "list"]
    assert line == top + 1 and stacked[top + 3].startswith("╰")
    assert not any("▀" in value or "▄" in value for value in stacked[top:top + 4])
    text = watch.render_frame(snapshot(rows), config, tmp_path, width=40, now=NOW)
    assert "1 planning-session request" in text and "▀" in text  # The squirrel stacks above the narrow bubble.
    assert "Planning request:" not in text and "D75 order" not in text
    idle = watch.render_frame(snapshot([]), config, tmp_path, width=160, now=NOW)
    # #2824: without requests there is no count or raw request text.
    assert "requests" not in idle and "Planning request" not in idle

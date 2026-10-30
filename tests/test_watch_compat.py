"""Patches at the original watch module must reach extracted implementations."""

from types import SimpleNamespace
from unittest.mock import ANY, Mock

from conftest import BASE_TIME
from foragent import cli, watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker


def test_collection_uses_replaced_facade_events(tmp_path, monkeypatch):
    events = [{"summary": "facade event"}]
    read_events = Mock(return_value=events)
    monkeypatch.setattr(watch, "recent_events", read_events)
    store, tracker, config = StateStore(tmp_path), FakeTracker(), Config()

    snapshot = watch.collect(cli._status(tracker, store, config), tracker, store, config, BASE_TIME)

    assert snapshot.events is events
    read_events.assert_called_once_with(store)


def test_cell_helpers_use_replaced_facade_helpers(monkeypatch):
    monkeypatch.setattr(watch, "_clean", lambda value: "facade text")
    assert watch._line("input", "bold") == [("facade text", "bold")]

    monkeypatch.setattr(watch, "_cells", lambda value: 2 * len(value))
    assert watch._fit([("abc", "bold")], 3) == [("a", "bold"), (" ", "road")]


def test_layout_uses_replaced_facade_detail_and_activity(tmp_path, monkeypatch):
    store, tracker, config = StateStore(tmp_path), FakeTracker(), Config()
    tracker.add_issue(1, "Working", labels=["running"])
    store.start_run(1, branch="issue-1", worktree="unused")
    snapshot = watch.collect(cli._status(tracker, store, config), tracker, store, config, BASE_TIME)
    detail = Mock(return_value="facade detail")
    monkeypatch.setattr(watch, "render_detail", detail)
    # A lane row shows the activity cell when it warns (○ stalled or gone, ? quiet).
    monkeypatch.setattr(watch, "_activity", lambda *args: "○ facade activity")

    frame = watch.render_frame(snapshot, config, tmp_path, selected="main")

    assert "facade detail" in frame
    assert "facade activity" in frame
    detail.assert_called_once_with(config, "main", tmp_path, done_count=0)


def test_live_run_uses_replaced_facade_frame_and_clock(tmp_path, monkeypatch):
    import curses

    store, tracker, config = StateStore(tmp_path), FakeTracker(), Config()
    snapshot = watch.Snapshot({"lanes": {}}, {}, None, 0, 0, None, [], BASE_TIME)
    frame = Mock(return_value=[[("facade live frame", "")], [("footer", "")]])
    screen = Mock()
    screen.getmaxyx.return_value = (24, 80)
    screen.getch.return_value = ord("q")
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(watch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "collect", lambda *args: snapshot)
    monkeypatch.setattr(watch, "_frame", frame)
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "wrapper", lambda terminal, *args: terminal(screen, *args))

    watch.run(lambda: {}, tracker, store, config, tmp_path, now=lambda: BASE_TIME)

    frame.assert_called_once_with(snapshot, config, tmp_path, tick=0, width=80,
                                  selected=None, now=BASE_TIME, layout={}, state=ANY)
    assert screen.addstr.call_args_list[0].args[:3] == (0, 0, "facade live frame")
    assert not (tmp_path / "watch.pid").exists()

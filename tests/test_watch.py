from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import cli, watch
from foragent.config import Config, ConfigError
from foragent.state import StateStore
from foragent.tracker import FakeTracker, RecordingTracker


def local_status(tracker, store, config=None):
    """Status reads only local records, so first record what the gate would observe."""
    config = config or Config()
    context = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC))
    RecordingTracker(tracker, context).refresh_issues()
    return cli._status(tracker, store, config)


NOW = datetime(2026, 10, 21, 9, tzinfo=UTC)


def snapshot(tracker, store, config):
    return watch.collect(local_status(tracker, store, config), tracker, store, config, NOW)


def finish(tracker, store, number, parent, lane="api"):
    tracker.add_issue(number, f"Finished task {number}", labels=[f"lane:{lane}"], state="closed")
    tracker.set_parent(number, parent)
    store.start_run(number, branch=f"issue-{number}", worktree="unused", lane=lane)
    store.update_run(number, last_progress_at=(NOW + timedelta(minutes=4)).isoformat())
    store.finish_run(number, "merged")


def test_order_progress_and_lane_totals_use_full_history_without_other_orders(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {"title": "Server"}}, max_parallel=2)
    tracker.add_issue(90, "Current order")
    tracker.add_issue(91, "Other order")
    for number in range(1, 14):
        finish(tracker, store, number, 90)
    finish(tracker, store, 14, 91)
    tracker.add_issue(15, "Active task", labels=["approved", "running", "lane:api"])
    tracker.set_parent(15, 90)
    store.start_run(15, branch="active", worktree="unused", lane="api")
    result = snapshot(tracker, store, config)
    assert len(result.status["history"]) == 10
    assert result.order.number == 90
    assert (result.completed, result.total) == (13, 14)
    assert [issue.number for issue in result.done["api"]] == list(range(1, 14))
    assert result.eta == NOW + timedelta(minutes=4)
    output = watch.render_frame(result, config, tmp_path)
    assert "13/14" in output and "✓ 13 done" in output and "Finished task 13" in output
    assert "Finished task 14" not in output


def test_road_keeps_dependency_queue_and_hit_only_flashes_active_block(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {"title": "Server"}})
    tracker.add_issue(1, "Working", labels=["approved", "running", "lane:api"])
    tracker.add_issue(2, "Ready", labels=["approved", "ready", "lane:api"])
    tracker.add_issue(3, "Waiting", labels=["approved", "lane:api"])
    store.start_run(1, branch="active", worktree="unused", lane="api")
    result = snapshot(tracker, store, config)
    output = watch.render_frame(result, config, tmp_path)
    assert "#2" in output and "#3" in output
    frames = [watch._frame(result, config, tmp_path, tick=tick, width=120, selected=None, now=NOW)
              for tick in range(4)]
    for tick, frame in enumerate(frames):
        spans = [span for line in frame for span in line]
        assert [(value, style) for value, style in spans if style == "hit"] == (
            [(" #1 ", "hit")] if tick == 2 else [])
        assert [(value, style) for value, style in spans if style == "queue"] == [
            (" #3 ", "queue"), (" #2 ", "queue")]


def test_open_order_after_last_child_and_no_order_are_renderable(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config()
    empty = watch.render_frame(snapshot(tracker, store, config), config, tmp_path)
    assert "no open order" in empty and "none yet" in empty
    tracker.add_issue(90, "Awaiting order closure")
    finish(tracker, store, 1, 90, lane="main")
    output = watch.render_frame(snapshot(tracker, store, config), config, tmp_path)
    assert "1/1" in output and "all done" in output


@pytest.mark.parametrize("language,idle_label,all_idle", [
    ("en", "idle lanes:", "all lanes are idle"),
    ("ko", "쉬는 레인:", "모든 레인이 쉬는 중"),
])
def test_idle_done_count_and_automatic_unfold_keep_original_lane_numbers(
    tmp_path, language, idle_label, all_idle,
):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language=language, lanes={
        "api": {"title": "Server", "emoji": "🔧"},
        "docs": {"title": "Docs", "emoji": "🧩"},
    })
    tracker.add_issue(90, "Order")
    finish(tracker, store, 1, 90, lane="docs")
    idle = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=80)
    folded = [line for line in idle.splitlines() if idle_label in line]
    assert len(folded) == 1
    assert "1 🔧 Server · 2 🧩 Docs ✓1 · 3 🧰" in folded[0]
    assert all_idle in idle and "1–3" in idle
    assert "▐" not in idle

    tracker.add_issue(2, "Next docs task", labels=["approved", "ready", "lane:docs"])
    active = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=80)
    assert all_idle not in active
    assert any("2 🧩 Docs" in line and "▐" in line for line in active.splitlines())
    folded = next(line for line in active.splitlines() if idle_label in line)
    assert "1 🔧 Server · 3 🧰" in folded and "Docs" not in folded
    assert "1–3" in active


@pytest.mark.parametrize("width", [40, 80, 120])
def test_work_table_has_one_row_per_busy_lane_with_unicode_ellipsis(tmp_path, width):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="ko", lanes={"api": {"title": "서버", "emoji": "🔧"}})
    tracker.add_issue(1, "긴 작업 이름 " * 30, labels=["approved", "ready", "lane:api"])
    frame = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=width)
    lines = frame.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("┌") and "지금 하는 일" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("└"))
    assert end - start == 3  # heading, column names, one work row, bottom border
    assert "…" in lines[start + 2]
    assert all(watch._cells(line) <= width for line in lines)


def test_live_liveness_crosses_ten_minutes_between_refreshes(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="ko")
    tracker.add_issue(1, "진행 중", labels=["approved", "running"])
    store.start_run(1, branch="active", worktree="unused")
    store.update_run(1, last_activity_at=(NOW - timedelta(seconds=599)).isoformat())
    # Use a frozen status clock, just as the CLI's live refresh does.
    status = local_status(tracker, store, config)
    status["lanes"]["main"]["last_activity_seconds"] = 599
    result = watch.collect(status, tracker, store, config, NOW)
    before = watch.render_frame(result, config, tmp_path, now=NOW)
    after = watch.render_frame(result, config, tmp_path, now=NOW + timedelta(seconds=2))
    assert "멈춘 듯" not in before
    assert "○ 멈춘 듯 10분" in after


@pytest.mark.parametrize("language,gone", [("en", "○ runner gone"), ("ko", "○ 실행기가 끝났어요")])
def test_live_runner_death_is_visible_without_refresh_and_permission_is_alive(tmp_path, monkeypatch, language, gone):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language=language)
    tracker.add_issue(1, "Working", labels=["running"])
    store.start_run(1, branch="active", worktree="unused")
    store.update_run(1, pid=12345)
    result = snapshot(tracker, store, config)
    result.status["lanes"]["main"]["last_activity_seconds"] = 0

    def inaccessible(pid, signal):
        assert (pid, signal) == (12345, 0)
        raise PermissionError

    monkeypatch.setattr(watch.os, "kill", inaccessible)
    assert "○" not in watch.render_frame(result, config, tmp_path)

    def dead(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr(watch.os, "kill", dead)
    assert gone in watch.render_frame(result, config, tmp_path)
    result.status["lanes"]["main"]["state"] = "blocked"
    assert gone not in watch.render_frame(result, config, tmp_path)


@pytest.mark.parametrize("stall_minutes,threshold", [(3, 180), (20, 600)])
def test_live_liveness_uses_configured_threshold_between_refreshes(tmp_path, stall_minutes, threshold):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(stall_minutes=stall_minutes)
    tracker.add_issue(1, "Working", labels=["running"])
    store.start_run(1, branch="active", worktree="unused")
    result = snapshot(tracker, store, config)
    result.status["lanes"]["main"]["last_activity_seconds"] = threshold - 1
    assert "○" not in watch.render_frame(result, config, tmp_path, now=NOW)
    assert "○ looks stalled" in watch.render_frame(result, config, tmp_path, now=NOW + timedelta(seconds=2))


def test_partial_event_append_and_missing_memory_have_usable_output(tmp_path):
    store = StateStore(tmp_path)
    events = [{"summary": f"Event {number}"} for number in range(7)]
    (tmp_path / "orch-events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events)
                                               + '{"summary": "incomplete')
    assert [event["summary"] for event in watch.recent_events(store)] == [f"Event {n}" for n in range(6, 1, -1)]
    config = Config(language="ko-KR", executor="claude", executor_model="inherited-model", lanes={"api": {}})
    detail = watch.render_detail(config, "api", tmp_path)
    assert "claude" in detail and "inherited-model" in detail and "아직 기억이 없어요" in detail


@pytest.mark.parametrize("width", [40, 80, 120, 180])
def test_unicode_layout_fits_resized_terminal_and_preserves_event_summary(tmp_path, width):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="ko", lanes={"api": {"title": "서버", "emoji": "🔧"}})
    result = snapshot(tracker, store, config)
    result.events = [{"summary": "긴 사건 기록도 잘리지 않고 마지막 내용까지 보여야 합니다 끝", "at": NOW.isoformat()}]
    output = watch.render_frame(result, config, tmp_path, width=width)
    assert all(watch._cells(line) <= width for line in output.splitlines())
    assert "합니다 끝 ✓" in output


@pytest.mark.parametrize("lane_count,keys", [(1, "11q"), (12, "12\n12\nq")])
def test_keyboard_toggles_detail_and_scrolls_it_into_view(tmp_path, monkeypatch, lane_count, keys):
    import curses

    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={f"lane-{n:02}": {"title": f"Lane {n}"} for n in range(lane_count)})
    result = snapshot(tracker, store, config)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)

    class Screen:
        def __init__(self):
            self.keys = iter(keys)
            self.frames = []
            self.current = []

        def keypad(self, value): pass
        def timeout(self, value): pass
        def getmaxyx(self): return 24, 120
        def erase(self): self.current = []
        def addstr(self, y, x, value, style): self.current.append(value)
        def refresh(self): self.frames.append("".join(self.current))
        def getch(self): return ord(next(self.keys))

    screen = Screen()
    watch._terminal(screen, lambda: result, result, config, tmp_path, lambda: NOW)
    assert "executor:" not in screen.frames[0]
    assert any("agent detail" in frame and "executor:" in frame for frame in screen.frames[1:-1])
    assert "executor:" not in screen.frames[-1]


def test_live_watch_without_a_terminal_explains_once_option(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ConfigError, match="--once"):
        watch.run(lambda: {}, FakeTracker(), StateStore(tmp_path), Config(), tmp_path)


def test_first_line_shows_snapshot_time_and_warns_when_nothing_was_read(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config()
    empty = watch.collect(cli._status(tracker, store, config), tracker, store, config, NOW)
    first = watch.render_frame(empty, config, tmp_path).splitlines()[0]
    assert first.startswith("⚠ Couldn't read the status") and "working ?" in first
    tracker.add_issue(1, "Queued", labels=["approved"])
    observed = local_status(tracker, store, config)
    first = watch.render_frame(watch.collect(observed, tracker, store, config, NOW),
                               config, tmp_path).splitlines()[0]
    local_time = datetime.fromisoformat(observed["as_of"]).astimezone().strftime("%H:%M")
    assert first.endswith(f"queued 1 · as of {local_time}") and "⚠" not in first

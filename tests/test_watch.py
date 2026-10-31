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
    store.update_run(number, last_progress_at=(NOW - timedelta(minutes=4)).isoformat())
    store.finish_run(number, "merged")


def test_order_progress_and_lane_totals_use_full_history_without_closed_orders(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {"title": "Server"}}, max_parallel=2)
    tracker.add_issue(90, "Current order")
    tracker.add_issue(91, "Other order", state="closed")
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
    # All 14 merges finished recently. The one approved issue left has no size label,
    # so it counts as size:m, learned from the 14 merged runs (started and finished
    # at the fixed clock, so they took no time).
    assert result.recent == 14 and result.eta == NOW
    assert result.eta_basis == {"m": 14}
    output = watch.render_frame(result, config, tmp_path)
    # The r57 bar counts today's 14 merges (both orders) against them plus the one running
    # issue; the old order totals stay in the snapshot for other consumers.
    assert "93% · 14 of 15 issues done" in output and "13/14" not in output
    assert "Finished task" not in output, "merged titles are not lane rows any more"


def test_a_lane_with_a_worker_shows_only_its_worker_row(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {"title": "Server"}}, view_glyphs="emoji")
    tracker.add_issue(1, "Working", labels=["approved", "running", "lane:api"])
    tracker.add_issue(2, "Ready", labels=["approved", "ready", "lane:api"])
    tracker.add_issue(3, "Waiting", labels=["approved", "lane:api"])
    store.start_run(1, branch="active", worktree="unused", lane="api")
    result = snapshot(tracker, store, config)
    output = watch.render_frame(result, config, tmp_path)
    lines = output.splitlines()
    # One lane block: its name row, the worker's acorn, then the queue on the next row.
    name = next(index for index, line in enumerate(lines) if line[14:].startswith("Server"))
    assert "◖█#1▌" in lines[name + 1]
    queue = next(line for line in lines[name:] if "next  " in line)
    assert "◖█#1" not in queue and [f"#{item['number']}" for item in result.status["lanes"]["api"]["queued"]] == [
        part.strip("◖█▌") for part in queue[14:].removeprefix("next  ").split("  ")]
    assert "#1 Working" not in output and "#2 Ready" not in output, "titles move into the click explanation"


def test_open_order_after_last_child_and_no_order_are_renderable(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config()
    empty = watch.render_frame(snapshot(tracker, store, config), config, tmp_path)
    # With no work, the bubble says so and no bar counts zero issues.
    assert "Nothing to do now. Tell the planning session about new work." in empty
    assert "issues done" not in empty and "none yet" not in empty and "now doing" not in empty
    tracker.add_issue(90, "Awaiting order closure")
    finish(tracker, store, 1, 90, lane="main")
    output = watch.render_frame(snapshot(tracker, store, config), config, tmp_path)
    assert "100% · 1 of 1 issue done" in output and "Just merged #1." in output


@pytest.mark.parametrize("language,done_words", [("en", "1 done"), ("ko", "1개 끝냄")])
def test_idle_lanes_have_one_row_each_and_a_queued_lane_waits_for_a_turn(tmp_path, language, done_words):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language=language, view_glyphs="ascii", lanes={
        "api": {"title": "Server", "emoji": "🔧"},
        "docs": {"title": "Docs", "emoji": "🧩"},
    })
    tracker.add_issue(90, "Order")
    finish(tracker, store, 1, 90, lane="docs")
    idle = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=80).splitlines()
    # Idle lanes share one block: each name once on the names row, today's count only where merged.
    rows = {name: [line for line in idle if name in line] for name in ("Server", "Docs")}
    assert [len(found) for found in rows.values()] == [1, 1] and rows["Server"] == rows["Docs"]
    assert "🧩 Docs v1" in rows["Docs"][0] and "🔧 Server v" not in rows["Server"][0]
    assert "▐" not in "\n".join(idle) and done_words not in "\n".join(idle)

    tracker.add_issue(2, "Next docs task", labels=["approved", "ready", "lane:docs"])
    active = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=80).splitlines()
    docs = [line for line in active if "[#2]" in line]
    assert len(docs) == 1 and ("next  " in docs[0] or "다음  " in docs[0])
    name = next(index for index, line in enumerate(active) if line.startswith("🧩 Docs"))
    # The waiting reason row comes from the recorded facts; its sentence always leads.
    assert active[name + 1].startswith("waiting its turn" if language == "en" else "순서를 기다려요")


@pytest.mark.parametrize("width", [40, 80, 120])
def test_a_long_title_never_wraps_its_row(tmp_path, width):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="ko", lanes={"api": {"title": "서버", "emoji": "🔧"}})
    tracker.add_issue(1, "긴 작업 이름 " * 30, labels=["approved", "ready", "lane:api"])
    lines = watch.render_frame(snapshot(tracker, store, config), config, tmp_path, width=width).splitlines()
    rows = [line for line in lines if "#1" in line]
    # The acorn carries the number alone; the title waits for the click explanation.
    assert len(rows) == 1 and "◖█#1▌" in rows[0] and "긴 작업" not in "\n".join(lines)
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


@pytest.mark.parametrize("language,gone", [("en", "○ ⚠ no worker"), ("ko", "○ ⚠ 작업자가 없어요")])
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
    assert gone not in watch.render_frame(result, config, tmp_path)

    def dead(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr(watch.os, "kill", dead)
    assert gone in watch.render_frame(result, config, tmp_path)
    result.status["lanes"]["main"]["state"] = "blocked"
    result.worker_states[1] = "blocked"  # A worker row follows its own issue's labels.
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
    assert "○ looks stalled" not in watch.render_frame(result, config, tmp_path, now=NOW)
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
    # The r56 bubble replaces the event log; the event itself stays in the snapshot. The usage
    # and hint rows are the last two rows, cut to the width instead of wrapped.
    assert "합니다 끝" not in output and result.events[0]["summary"].endswith("합니다 끝")
    assert "지금은 할 일이 없어요." in output.replace("\n", " ") or width < 80
    lines = output.splitlines()
    assert lines[-2].startswith("Claude —") and "동시 " not in lines[-2]
    assert lines[-1].startswith("도토리를 누르면")
    if width >= 120:
        assert lines[-1].endswith("q 끝내기") and "keep 80%" in lines[-2]


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
        def move(self, y, x): pass
        def clrtoeol(self): pass
        def clrtobot(self): pass

        def refresh(self):
            # The view erases once and then paints in place: each refresh ends a frame.
            self.frames.append("".join(self.current))
            self.current = []
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


def test_header_warns_when_nothing_was_read_and_summary_keeps_the_snapshot_time(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config()
    empty = watch.collect(cli._status(tracker, store, config), tracker, store, config, NOW)
    frame = watch.render_frame(empty, config, tmp_path)
    # The top line puts the warning before the status (lane_header).
    assert frame.splitlines()[0].startswith("⚠ Couldn't read the status")
    tracker.add_issue(1, "Queued", labels=["approved"])
    observed = local_status(tracker, store, config)
    frame = watch.render_frame(watch.collect(observed, tracker, store, config, NOW), config, tmp_path)
    assert "Couldn't read the status" not in frame
    assert frame.splitlines()[0].startswith("● "), "with the status read, the top line has no warning"


@pytest.mark.parametrize('language,label', [('en', 'waiting for shared files'), ('ko', '공유 파일 기다려요')])
def test_overlap_wait_activity_uses_translated_waiting_phase(language, label):
    from foragent.watch_layout import _activity

    row = {'state': 'working', 'phase': 'overlap-wait', 'last_activity_seconds': 0}
    assert _activity(row, language, 0, 10, None, phase_seconds=120) == (
        f'● {label} ' + ('2 min' if language == 'en' else '2분')
    )

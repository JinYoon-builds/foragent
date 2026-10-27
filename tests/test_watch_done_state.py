from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import cli, watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, RecordingTracker
from foragent.watch_sections import activity, done, summary

NOW = datetime(2026, 10, 21, 9, tzinfo=UTC)


def collected(tracker, store, config):
    context = SimpleNamespace(store=store, config=config, now=lambda: NOW)
    RecordingTracker(tracker, context).refresh_issues()
    return watch.collect(cli._status(tracker, store, config), tracker, store, config, NOW)


def finish(tracker, store, number, parent, lane="api", title=None):
    tracker.add_issue(number, title or f"Finished task {number}", labels=[f"lane:{lane}"], state="closed")
    tracker.set_parent(number, parent)
    store.start_run(number, branch=f"issue-{number}", worktree="unused", lane=lane)
    store.finish_run(number, "merged")


def plain(lines):
    return ["".join(value for value, style in line) for line in lines]


def test_collect_reads_each_done_issue_merge_time_from_local_records(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {"title": "Server"}})
    tracker.add_issue(90, "Order")
    finish(tracker, store, 1, 90)
    finish(tracker, store, 2, 90)
    rows = store.local_issues
    times = {1: "2026-10-21T08:00:00+00:00", 2: "not a time"}
    store.local_issues = lambda: [dict(row, merged_at=times.get(row["number"])) for row in rows()]
    result = collected(tracker, store, config)
    assert result.merged_at == {1: datetime(2026, 10, 21, 8, tzinfo=UTC)}


def test_done_list_is_newest_first_then_unknown_by_number_and_counts_the_rest():
    issues = {number: Issue(number, f"Task {number}", "", frozenset(), "closed") for number in range(1, 12)}
    snapshot = watch.Snapshot(
        {"lanes": {}, "summary": {}, "active_runs": []},
        {"api": [issues[n] for n in (1, 2, 3, 4, 5, 6)], "docs": [issues[n] for n in (7, 8, 9, 10, 11)]},
        None, 11, 11, None, [], NOW,
        merged_at={3: NOW - timedelta(hours=2), 9: NOW - timedelta(hours=1), 5: NOW - timedelta(hours=3)})
    config = Config(lanes={"api": {"title": "Server", "emoji": "🔧"}, "docs": {"title": "Docs", "emoji": "🧩"}})
    lines = plain(done.render(snapshot, config, None, width=80))
    assert lines[0] == "This order is all done ✓"
    numbers = [int(line.split()[0][1:]) for line in lines[1:9]]
    assert numbers == [9, 3, 5, 11, 10, 8, 7, 6]
    assert lines[1] == "#9 Task 9 — 🧩 Docs"
    assert lines[9] == "+3 more"
    ko = plain(done.render(snapshot, replace(config, language="ko"), None, width=80))
    assert ko[0] == "이번 주문을 모두 끝냈어요 ✓" and ko[-1] == "+3개 더"


def test_done_list_cuts_long_titles_but_keeps_number_and_lane():
    issue = Issue(7, "word " * 40, "", frozenset(), "closed")
    snapshot = watch.Snapshot({"lanes": {}, "summary": {}, "active_runs": []}, {"api": [issue]},
                              None, 1, 1, None, [], NOW)
    config = Config(lanes={"api": {"title": "Server", "emoji": "🔧"}})
    line = plain(done.render(snapshot, config, None, width=40))[1]
    assert line.startswith("#7 word") and "…" in line and line.endswith("— 🔧 Server")
    assert watch._cells(line) <= 40


def test_done_section_is_empty_until_every_child_is_closed():
    base = watch.Snapshot({"lanes": {}, "summary": {}, "active_runs": []}, {}, None, 2, 3, None, [], NOW)
    assert done.render(base, Config(), None, width=80) == []
    assert done.render(replace(base, completed=0, total=0), Config(), None, width=80) == []


def test_summary_hides_only_zero_counts_unless_all_zero_or_unknown():
    counts = lambda **values: {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0, **values}
    assert summary._counts(counts(working=2, queued=1), "en") == "working 2 · queued 1"
    assert summary._counts(counts(waiting_human=1), "ko") == "확인할 일 1"
    assert summary._counts(counts(), "en") == "working 0 · waiting for you 0 · queued 0"
    assert summary._counts(counts(working=1, queued=None), "en") == "working 1 · waiting for you 0 · queued ?"


def test_only_the_events_panel_remains_and_takes_the_full_width(tmp_path):
    config = Config(lanes={"api": {"title": "Server"}})
    status = {"lanes": {"api": {"slug": "api", "lane_display": "🧰 Server", "state": "idle", "issue": None,
                                "queued": [], "running": [], "ready": []}},
              "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0}, "active_runs": []}
    event = {"at": NOW.isoformat(), "kind": "note", "summary": "checked the board", "issue": None}
    snapshot = watch.Snapshot(status, {}, None, 0, 0, None, [event], NOW)
    lines = plain(activity.render(snapshot, config, tmp_path, width=120, now=NOW))
    assert len(lines) == 3 and all(watch._cells(line) == 120 for line in lines)
    assert "handled by the orchestrator" in lines[0] and "now doing" not in "".join(lines)
    assert activity.render(replace(snapshot, events=[]), config, tmp_path, width=120, now=NOW) == []


def test_each_idle_lane_has_one_row_with_its_whole_name_and_done_count(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    lanes = {f"lane-{i}": {"title": f"Long lane name {i}", "emoji": "🧩"} for i in range(6)}
    config = Config(lanes=lanes, view_glyphs="emoji")
    tracker.add_issue(90, "Order")
    finish(tracker, store, 1, 90, lane="lane-0")
    frame = watch.render_frame(collected(tracker, store, config), config, tmp_path, width=80)
    assert all(watch._cells(line) <= 80 for line in frame.splitlines())
    for lane in lanes.values():
        rows = [line for line in frame.splitlines() if line.startswith("💤") and lane["title"] in line]
        assert len(rows) == 1, lane
    assert "Long lane name 0 ✓ 1 done" in frame and "✓1" not in frame
    assert "▶" not in frame


def test_done_item_with_a_lane_name_wider_than_the_line_wraps_instead_of_cutting():
    issue = Issue(7, "Task", "", frozenset(), "closed")
    lane_title = "Extremely long lane name " * 3
    snapshot = watch.Snapshot({"lanes": {}, "summary": {}, "active_runs": []}, {"api": [issue]},
                              None, 1, 1, None, [], NOW)
    config = Config(lanes={"api": {"title": lane_title.strip(), "emoji": "🔧"}})
    lines = plain(done.render(snapshot, config, None, width=40))
    assert all(watch._cells(line) <= 40 for line in lines)
    assert " ".join(" ".join(lines[1:]).split()).endswith(" ".join(lane_title.split()))

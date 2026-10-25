"""Numbered worker rows (#477) and evidence-aged claims (D67) in the lane view."""

from datetime import timedelta
import json
import os

from foragent import watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_watch import NOW, local_status

LANES = {"orch": {"paths": ["src/**"], "title": "orch", "emoji": "🧭"},
         "docs": {"paths": ["docs/**"], "title": "docs", "emoji": "📝"}}


def workers_case(tmp_path, starts):
    """Running issues in lane orch, started at the given minute offsets."""
    clock = [NOW]
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: clock[0])
    config = Config(language="en", lanes={slug: dict(lane) for slug, lane in LANES.items()}, max_parallel=5)
    for number, minutes in starts.items():
        tracker.add_issue(number, f"Task {number}", labels=["approved", "running", "lane:orch"])
        clock[0] = NOW + timedelta(minutes=minutes)
        store.start_run(number, branch=f"issue-{number}", worktree="unused", lane="orch")
    clock[0] = NOW + timedelta(minutes=30)
    return tracker, store, config, clock


def frame(tracker, store, config, clock, width=160):
    snapshot = watch.collect(local_status(tracker, store, config), tracker, store, config, clock[0])
    return watch.render_frame(snapshot, config, store.root, width=width, now=clock[0])


def rows_with(text, needle):
    return [row for row in text.splitlines() if needle in row]


def test_workers_are_numbered_in_start_order_not_issue_order(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 5})
    text = frame(tracker, store, config, clock)
    first, second = rows_with(text, "orch 1"), rows_with(text, "orch 2")
    assert len(first) == 1 and "#7" in first[0] and "#3" not in first[0], text
    assert len(second) == 1 and "#3" in second[0] and "#7" not in second[0], text
    assert "×" not in text


def test_start_time_ties_break_by_issue_number(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 0})
    text = frame(tracker, store, config, clock)
    assert "#3" in rows_with(text, "orch 1")[0] and "#7" in rows_with(text, "orch 2")[0], text


def test_numbers_are_positions_among_current_workers(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1, 3: 2})
    store.finish_run(1, "merged")
    tracker.close_issue(1)
    text = frame(tracker, store, config, clock)
    assert "#2" in rows_with(text, "orch 1")[0] and "#3" in rows_with(text, "orch 2")[0], text
    assert not rows_with(text, "orch 3")
    store.finish_run(2, "merged")
    tracker.close_issue(2)
    text = frame(tracker, store, config, clock)
    assert not rows_with(text, "orch 1") and "#3" in text, "one worker left shows the plain lane name"


def test_queue_and_done_line_stay_on_the_first_worker_and_table_uses_plain_name(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1})
    tracker.add_issue(9, "Waiting task", labels=["approved", "ready", "lane:orch"])
    text = frame(tracker, store, config, clock)
    lines = text.splitlines()
    first = next(index for index, row in enumerate(lines) if "orch 1" in row)
    assert "#9" in lines[first] and "#9" not in rows_with(text, "orch 2")[0], text
    assert "done" in lines[first + 1] and "orch 2" in lines[first + 2], text
    table = [row for row in lines if row.startswith("│🧭 orch ")]
    assert len(table) == 2 and all("orch 1" not in row and "orch 2" not in row for row in table), text


def test_orchestrator_claim_needs_a_fresh_heartbeat(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0})
    assert "orchestrator is off" in frame(tracker, store, config, clock), "missing heartbeat with work"
    path = store.root / "orch-heartbeat.json"
    path.write_text(json.dumps({"at": (clock[0] - timedelta(seconds=5)).isoformat()}))
    text = frame(tracker, store, config, clock)
    assert "watching" in text and "orchestrator is off" not in text
    path.write_text(json.dumps({"at": (clock[0] - timedelta(seconds=31)).isoformat()}))
    text = frame(tracker, store, config, clock)
    assert "orchestrator is off" in text and "watching" not in text


def test_idle_project_without_heartbeat_claims_nothing(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {})
    text = frame(tracker, store, config, clock)
    assert "watching" not in text and "⚠" not in text


def test_stale_gate_heartbeat_warns_on_the_first_line(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0})
    path = store.root / "gate-heartbeat.json"
    path.write_text(json.dumps({"at": (clock[0] - timedelta(minutes=14)).isoformat()}))
    assert "⚠" not in frame(tracker, store, config, clock).splitlines()[0]
    path.write_text(json.dumps({"at": (clock[0] - timedelta(minutes=16)).isoformat()}))
    assert "gate hasn't run for 16 min" in frame(tracker, store, config, clock).splitlines()[0]


def test_log_age_decides_working_or_quiet(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0})
    store.update_run(1, last_activity_at=clock[0].isoformat())
    log = store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("progress\n")
    for minutes, expected in ((9, None), (10, "quiet for 10 min")):
        modified = (clock[0] - timedelta(minutes=minutes)).timestamp()
        os.utime(log, (modified, modified))
        row = rows_with(frame(tracker, store, config, clock), "#1 Task 1")[0]
        assert ("quiet" not in row) if expected is None else (expected in row), row


def test_table_lists_several_workers_in_start_order(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 5})
    table = [row for row in frame(tracker, store, config, clock).splitlines() if row.startswith("│🧭 orch ")]
    assert "#7" in table[0] and "#3" in table[1], table


def test_long_lane_name_gives_way_to_the_worker_number(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1})
    config.lanes["orch"]["title"] = "orchestration"
    text = frame(tracker, store, config, clock)
    first = [row for row in text.splitlines() if " 1 ▸ " in row and "#1" in row]
    second = [row for row in text.splitlines() if " 2 ▸ " in row and "#2" in row]
    assert len(first) == 1 and len(second) == 1, text
    assert "…" in first[0] and "#2" not in first[0] and "#1" not in second[0], text


def test_each_worker_row_shows_its_own_state(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1})
    tracker.add_labels(1, ["blocked"])
    tracker.add_labels(2, ["blocked"])
    text = frame(tracker, store, config, clock)
    for worker in ("orch 1", "orch 2"):
        row = rows_with(text, worker)[0]
        assert "●" in row and "⛏" not in row, text
    tracker.remove_labels(1, ["blocked"])
    tracker.remove_labels(2, ["blocked"])
    tracker.add_labels(2, ["needs-human"])
    text = frame(tracker, store, config, clock)
    assert "⛏" in rows_with(text, "orch 1")[0] and "✋" not in rows_with(text, "orch 1")[0], text
    assert "✋" in rows_with(text, "orch 2")[0] and "⛏" not in rows_with(text, "orch 2")[0], text

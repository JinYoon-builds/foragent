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
    config = Config(language="en", lanes={slug: dict(lane) for slug, lane in LANES.items()}, max_parallel=5,
                    view_glyphs="emoji")
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


def acorns_in(text):
    """Issue numbers of the sideways acorns on the frame's rows, in order."""
    import re

    return [int(number) for row in text.splitlines() for number in re.findall(r"[◖◜◟]█#(\d+)", row)]


def explained(tracker, store, config, clock, number, width=160):
    """The bubble rows after clicking an acorn (the lane scene explains it in place)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
    from test_lane_m2_contract import bubble

    snapshot = watch.collect(local_status(tracker, store, config), tracker, store, config, clock[0])
    return bubble(watch.render_frame(snapshot, config, store.root, width=width, now=clock[0],
                                     state={"explanation_target": ("acorn", number)}))


def test_workers_are_numbered_in_start_order_not_issue_order(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 5})
    text = frame(tracker, store, config, clock)
    # One squirrel per lane: both workers share its acorn row, in start order, with no numbering.
    assert acorns_in(text) == [7, 3], text
    assert len(rows_with(text, "◖█#7")) == 1 and "◖█#3" in rows_with(text, "◖█#7")[0]
    assert "×" not in text and "orch 1" not in text and "orch 2" not in text


def test_start_time_ties_break_by_issue_number(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 0})
    text = frame(tracker, store, config, clock)
    assert acorns_in(text) == [3, 7], text


def test_numbers_are_positions_among_current_workers(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1, 3: 2})
    store.finish_run(1, "merged")
    tracker.close_issue(1)
    text = frame(tracker, store, config, clock)
    assert acorns_in(text) == [2, 3], text
    store.finish_run(2, "merged")
    tracker.close_issue(2)
    text = frame(tracker, store, config, clock)
    assert acorns_in(text) == [3] and "orch 1" not in text, "one worker left keeps the plain lane name"


def test_a_lane_with_workers_shows_only_their_rows(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1})
    tracker.add_issue(9, "Waiting task", labels=["approved", "ready", "lane:orch"])
    text = frame(tracker, store, config, clock)
    # The waiting issue is on the lane's queue row, after the workers' acorn row.
    waiting = rows_with(text, "◖█#9▌")
    assert len(waiting) == 1 and "next  " in waiting[0] and "#1" not in waiting[0], text
    current = rows_with(text, "◖█#1")
    assert len(current) == 1 and "◖█#2" in current[0], text
    # No work table or orchestrator box lists issue numbers in boxed rows.
    assert not any(row.startswith(("│", "┌")) and "#" in row for row in text.splitlines()), "no work table"


def test_orchestrator_claim_needs_a_fresh_heartbeat(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0})
    assert "orchestrator is off" in frame(tracker, store, config, clock), "missing heartbeat with work"
    path = store.root / "orch-heartbeat.json"
    path.write_text(json.dumps({"at": (clock[0] - timedelta(seconds=5)).isoformat()}))
    text = frame(tracker, store, config, clock)
    # A fresh watch is no news: the evidence row only warns.
    assert "orchestrator is off" not in text and "⚠" not in text
    path.write_text(json.dumps({"at": (clock[0] - timedelta(seconds=31)).isoformat()}))
    text = frame(tracker, store, config, clock)
    assert "orchestrator is off" in text


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
        # The warning stands beside the acorn, and its explanation repeats it.
        row = rows_with(frame(tracker, store, config, clock), "◖█#1")[0]
        assert ("quiet" not in row) if expected is None else (expected in row), row
        words = explained(tracker, store, config, clock, 1)
        assert ("quiet" not in words) if expected is None else (expected in words), words


def test_rows_list_several_workers_in_start_order(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {7: 0, 3: 5})
    rows = rows_with(frame(tracker, store, config, clock), "◖█#")
    assert len(rows) == 1 and rows[0].index("#7") < rows[0].index("#3"), rows


def test_each_worker_row_shows_its_own_state(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 1})
    tracker.add_labels(1, ["blocked"])
    tracker.add_labels(2, ["blocked"])
    text = frame(tracker, store, config, clock)
    # A blocked worker is a grey wait acorn, never a question; its click names the orchestrator.
    assert "◖█#1 wait▌" in text and "◖█#2 wait▌" in text, text
    assert "handled by the orchestrator" in explained(tracker, store, config, clock, 1)
    tracker.remove_labels(1, ["blocked"])
    tracker.remove_labels(2, ["blocked"])
    tracker.add_labels(2, ["needs-human"])
    text = frame(tracker, store, config, clock)
    assert "◖█#1 prep▌" in text and "◖█#2 question▌" in text, text


def test_elapsed_counts_from_each_runs_start(tmp_path):
    tracker, store, config, clock = workers_case(tmp_path, {1: 0, 2: 25})
    # The elapsed time lives in each acorn's explanation, counted from its own run's start.
    assert "30m in this step" in explained(tracker, store, config, clock, 1)
    assert "5m in this step" in explained(tracker, store, config, clock, 2)

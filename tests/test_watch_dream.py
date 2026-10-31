"""The dreaming lane row and the status JSON's per-lane 'dreaming' fact (#190)."""

from dataclasses import replace
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_squirrel_running import plain_frame  # noqa: E402
from test_view_done_state import done_snapshot  # noqa: E402

from foragent.state import StateStore  # noqa: E402
from foragent.status import collect as status_collect  # noqa: E402
from foragent.watch import render_frame  # noqa: E402


def dreaming(snapshot, lane, **row):
    lanes = {slug: dict(value) for slug, value in snapshot.status["lanes"].items()}
    lanes[lane].update(dreaming=True, **row)
    return replace(snapshot, status={**snapshot.status, "lanes": lanes})


def lane_line(frame, config, lane):
    rows = [line.strip() for line in frame.splitlines() if config.lanes[lane]["title"] in line]
    assert rows, frame
    return rows


def test_status_json_lane_rows_follow_the_running_dream_record(tmp_path):
    baseline, config = done_snapshot(count=2)
    first, second = list(config.lanes)
    store = StateStore(tmp_path / "state", now=lambda: baseline.captured_at)
    assert {slug: row["dreaming"] for slug, row in status_collect(store, config, baseline.captured_at)["lanes"].items()
            if slug in config.lanes} == {first: False, second: False}
    store.start_dream(first, worktree=tmp_path / "dream")
    lanes = status_collect(store, config, baseline.captured_at)["lanes"]
    assert lanes[first]["dreaming"] is True and lanes[second]["dreaming"] is False
    assert lanes[first]["state"] == "idle", "a dream holds no issue"
    json.dumps(lanes)
    store.finish_dream(first, "dreamed")
    assert status_collect(store, config, baseline.captured_at)["lanes"][first]["dreaming"] is False


def test_dreaming_row_keeps_the_done_count_in_order_and_drops_it_at_zero(tmp_path):
    snapshot, config = done_snapshot(count=2, language="en")
    config = replace(config, view_glyphs="emoji")
    first, second = list(config.lanes)
    snapshot = replace(dreaming(snapshot, first), today_done={first: 2, second: 0})
    frame = plain_frame(render_frame(dreaming(snapshot, second), config, tmp_path, width=80))
    assert lane_line(frame, config, first)[0] == "💭 🧩 Agent0 dreaming · ✓ 2 done"
    assert lane_line(frame, config, second)[0] == "💭 🧩 Agent1 dreaming"


def test_ascii_glyphs_draw_the_bubble_as_brackets(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="ascii")
    first = list(config.lanes)[0]
    frame = plain_frame(render_frame(dreaming(snapshot, first), config, tmp_path, width=80))
    assert lane_line(frame, config, first)[0] == "[~] 🧩 담당0 꿈꾸는 중 · ✓ 2개 끝냄"
    assert "💭" not in frame


def test_lane_with_waiting_work_shows_its_normal_rows_without_a_bubble(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="emoji")
    first = list(config.lanes)[0]
    queued = [{"number": 7, "title": "Next work"}]
    frame = plain_frame(render_frame(dreaming(snapshot, first, queued=queued), config, tmp_path, width=80))
    assert "💭" not in frame and "꿈꾸는 중" not in frame
    assert any("#7" in line for line in lane_line(frame, config, first)), frame


def test_unfolding_a_dreaming_lane_lists_its_finished_issues_below_the_bubble(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="emoji")
    first = list(config.lanes)[0]
    state = {"unfolded": [first]}
    frame = plain_frame(render_frame(dreaming(snapshot, first), config, tmp_path, width=80, state=state))
    # The unbounded frame (shell) also keeps the done list, whose rows name the lane too.
    rows = [row for row in lane_line(frame, config, first) if not row.startswith("#")]
    assert rows[0].startswith("💭") and len(rows) == 3, frame
    assert "#1" in rows[1] and "#2" in rows[2]

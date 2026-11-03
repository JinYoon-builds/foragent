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
    rows = [line[14:].strip() if len(line) > 14 and not line[:14].strip("▀▄ ") else line.strip()
            for line in frame.splitlines() if config.lanes[lane]["title"] in line]
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
    # The shared idle block: each name, then ✓N only when N > 0, then 💭 while it dreams.
    row = lane_line(frame, config, first)
    assert row == lane_line(frame, config, second) and len(row) == 1
    assert row[0].endswith("🧩 Agent0 ✓2 💭 · 🧩 Agent1 💭"), row
    assert "dreaming" not in frame


def test_ascii_glyphs_draw_the_bubble_as_brackets(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="ascii")
    first = list(config.lanes)[0]
    frame = plain_frame(render_frame(dreaming(snapshot, first), config, tmp_path, width=80))
    # A hand-built snapshot has no today counts, so only the ASCII dream mark follows the name.
    assert lane_line(frame, config, first)[0].startswith("🧩 담당0 [~] | 🧩 담당1")
    assert "💭" not in frame and "꿈꾸는 중" not in frame


def test_lane_with_waiting_work_shows_its_normal_rows_without_a_bubble(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="emoji")
    first = list(config.lanes)[0]
    queued = [{"number": 7, "title": "Next work"}]
    frame = plain_frame(render_frame(dreaming(snapshot, first, queued=queued), config, tmp_path, width=80))
    assert "💭" not in frame and "꿈꾸는 중" not in frame
    # A lane with waiting work has its own block: its name row, then #7 on its queue row.
    name = next(index for index, line in enumerate(frame.splitlines()) if config.lanes[first]["title"] in line
                and "area0/**" in line)
    assert any("◖█#7▌" in line for line in frame.splitlines()[name:name + 5]), frame


def test_unfolding_a_dreaming_lane_lists_its_finished_issues_below_the_bubble(tmp_path):
    snapshot, config = done_snapshot(count=2)
    config = replace(config, view_glyphs="emoji")
    first = list(config.lanes)[0]
    state = {"unfolded": [first]}
    frame = plain_frame(render_frame(dreaming(snapshot, first), config, tmp_path, width=80, state=state))
    # Lane acorns replace finished rows: unfolding changes nothing, the lane sleeps and dreams.
    assert frame == plain_frame(render_frame(dreaming(snapshot, first), config, tmp_path, width=80))
    assert lane_line(frame, config, first)[0].count("💭") == 1
    assert "#1" not in frame and "#2" not in frame

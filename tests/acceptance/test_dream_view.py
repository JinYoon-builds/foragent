"""Acceptance test for dream-view. Locked by Foragent; do not edit."""

from dataclasses import asdict

import pytest

from test_local_view import record_gh
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_done_state import done_snapshot


@pytest.fixture(autouse=True)
def dream_view_offline(view_offline, monkeypatch):
    return record_gh(monkeypatch)


def dreaming_snapshot(lane_index=0):
    from foragent.state import Run

    snapshot, config = done_snapshot(count=2)
    lane = list(config.lanes)[lane_index]
    at = snapshot.captured_at.isoformat()
    run = asdict(Run(901, "", "", None, at, at, lane=lane))
    snapshot.status["active_runs"] = [{**run, "kind": "dream"}]
    return snapshot, config, lane


@pytest.mark.parametrize("width,lane_index", [(56, 0), (44, 1)])
def test_dream_view_active_dream_has_one_bubble_above_its_sleeping_lane(
    tmp_path, dream_view_offline, width, lane_index,
):
    from foragent.watch import render_frame

    snapshot, config, lane = dreaming_snapshot(lane_index)
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=width))
    assert frame.count("💭") == 1, "An active dream must mark exactly its own lane"
    lines = frame.splitlines()
    marker = next(index for index, line in enumerate(lines) if "💭" in line)
    nearby = lines[max(0, marker - 1):marker + 6]
    assert any(config.lanes[lane]["title"] in line for line in nearby), frame
    assert any("▀" in line or "▄" in line for line in lines[marker + 1:marker + 6]), (
        "The dream bubble belongs above the lane squirrel", frame)
    # Reuse squirrel-running's public pose contract; dreams retain the sleeping pose.
    from foragent.watch import lane_pose

    assert lane_pose(snapshot.status["lanes"][lane], snapshot, snapshot.captured_at) == "sleep"
    assert dream_view_offline == []


@pytest.mark.parametrize("lane_index", [0, 1])
def test_dream_view_text_mode_names_the_dreaming_lane_and_clears_on_completion(
    tmp_path, dream_view_offline, lane_index,
):
    from foragent.watch import render_frame

    snapshot, config, lane = dreaming_snapshot(lane_index)
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=43))
    assert any(config.lanes[lane]["title"] in line and "꿈꾸는 중 💭" in line
               for line in frame.splitlines()), frame
    assert frame.count("💭") == 1
    assert "▀" not in frame and "▄" not in frame
    run = snapshot.status["active_runs"].pop()
    snapshot.status["history"] = [{**run, "status": "finished", "outcome": "dreamed"}]
    after = plain_frame(render_frame(snapshot, config, tmp_path, width=43))
    assert "💭" not in after and "꿈꾸는 중" not in after
    assert dream_view_offline == []


def test_dream_view_sleep_without_an_active_dream_has_no_bubble(
    tmp_path, dream_view_offline,
):
    from foragent.watch import render_frame

    snapshot, config, _ = dreaming_snapshot()
    run = snapshot.status["active_runs"].pop()
    # Idle, completed dream, and an ordinary active run are all negative controls.
    for active, history in (
        ([], []),
        ([], [{**run, "status": "finished", "outcome": "dreamed"}]),
        ([{**run, "kind": "issue"}], []),
    ):
        snapshot.status.update(active_runs=active, history=history)
        for width in (56, 44, 43):
            frame = plain_frame(render_frame(snapshot, config, tmp_path, width=width))
            assert "💭" not in frame and "꿈꾸는 중" not in frame
    assert dream_view_offline == []

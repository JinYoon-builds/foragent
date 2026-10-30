"""Acceptance test for dream-view. Locked by Foragent; do not edit."""

from dataclasses import replace

import pytest

from test_local_view import record_gh
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_done_state import done_snapshot


@pytest.fixture(autouse=True)
def dream_view_offline(view_offline, monkeypatch):
    return record_gh(monkeypatch)


def dream_snapshot(store, config, now):
    """Read persisted dream records through the same snapshot boundary as the view."""
    from foragent.status import collect as status_collect
    from foragent.tracker import FakeTracker
    from foragent.watch import collect

    return collect(status_collect(store, config, now), FakeTracker(), store, config, now)


def dreaming_snapshot(tmp_path, lane_index=0, *, language="ko", active=True):
    from foragent.state import StateStore

    baseline, config = done_snapshot(count=2, language=language)
    config = replace(config, view_glyphs="emoji")
    lane = list(config.lanes)[lane_index]
    store = StateStore(tmp_path / "state", now=lambda: baseline.captured_at)
    if active:
        store.start_dream(lane, worktree=tmp_path / "dream")
    return dream_snapshot(store, config, baseline.captured_at), config, lane, store


def lane_row(frame, config, lane):
    lines = frame.splitlines()
    rows = [line for line in lines if config.lanes[lane]["title"] in line]
    assert len(rows) == 1, f"{lane} must occupy exactly one text row: {frame}"
    # The one big header squirrel is allowed; sprites among/below lane rows are not.
    first_lane = next(index for index, line in enumerate(lines)
                      if any(item["title"] in line for item in config.lanes.values()))
    lane_area = "\n".join(lines[first_lane:])
    assert "▀" not in lane_area and "▄" not in lane_area, "dreams must not add lane sprites"
    return rows[0].lstrip()


@pytest.mark.parametrize("width,lane_index", [(56, 0), (44, 1)])
def test_dream_view_active_dream_replaces_only_its_idle_lane_row(
    tmp_path, dream_view_offline, width, lane_index,
):
    from foragent.watch import render_frame

    snapshot, config, lane, store = dreaming_snapshot(tmp_path, lane_index)
    assert store.get_dream(lane).status == "running"
    assert snapshot.status["active_runs"] == [], "a dream is not an issue worker"
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=width))
    row = lane_row(frame, config, lane)
    assert row.startswith("💭") and "꿈꾸는 중" in row, (
        "the running dream record must reach its lane row through the snapshot", frame)
    assert "💤" not in row and frame.count("💭") == 1
    for other in config.lanes:
        if other != lane:
            assert lane_row(frame, config, other).startswith("💤"), frame
    assert dream_view_offline == []


@pytest.mark.parametrize("lane_index", [0, 1])
@pytest.mark.parametrize("language,words", [("ko", "꿈꾸는 중"), ("en", "dreaming")], ids=["ko", "en"])
def test_dream_view_text_mode_names_the_dreaming_lane_and_clears_on_completion(
    tmp_path, dream_view_offline, lane_index, language, words,
):
    from foragent.watch import render_frame

    snapshot, config, lane, store = dreaming_snapshot(tmp_path, lane_index, language=language)
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=43))
    row = lane_row(frame, config, lane)
    assert row.startswith("💭") and words in row, frame
    assert frame.count("💭") == 1
    store.finish_dream(lane, "dreamed")
    assert store.get_dream(lane).status == "finished"
    # Rendering must use the captured facts, not reread the changed store.
    assert plain_frame(render_frame(snapshot, config, tmp_path, width=43)) == frame
    completed = dream_snapshot(store, config, snapshot.captured_at)
    after = plain_frame(render_frame(completed, config, tmp_path, width=43))
    assert lane_row(after, config, lane).startswith("💤"), after
    assert "💭" not in after and words not in after
    assert dream_view_offline == []


def test_dream_view_sleep_without_an_active_dream_has_no_bubble(
    tmp_path, dream_view_offline,
):
    from foragent.watch import render_frame

    snapshot, config, lane, store = dreaming_snapshot(tmp_path, active=False)
    # Idle, completed dream, and an ordinary active run are all negative controls.
    snapshots = [(snapshot, "💤")]
    store.start_dream(lane, worktree=tmp_path / "dream")
    store.finish_dream(lane, "dreamed")
    snapshots.append((dream_snapshot(store, config, snapshot.captured_at), "💤"))
    store.start_run(901, branch="foragent/issue-901", worktree=store.worktree_path(901), lane=lane)
    snapshots.append((dream_snapshot(store, config, snapshot.captured_at), "🐿"))
    for snapshot, glyph in snapshots:
        for width in (56, 44, 43):
            frame = plain_frame(render_frame(snapshot, config, tmp_path, width=width))
            assert "💭" not in frame and "꿈꾸는 중" not in frame
            assert lane_row(frame, config, lane).startswith(glyph), frame
    assert dream_view_offline == []

"""Acceptance test for squirrel-done. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta

from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_done_state import done_snapshot


def test_squirrel_done_fireworks_stop_within_five_seconds_then_frames_are_identical(tmp_path):
    from foragent.watch import FIREWORKS_SECONDS, render_frame

    assert 0 < FIREWORKS_SECONDS <= 5
    snapshot, config = done_snapshot()
    # A tick is the live renderer's existing 0.3-second animation step.
    def frame(tick):
        return render_frame(snapshot, config, tmp_path, tick=tick, width=100,
                            now=snapshot.captured_at + timedelta(seconds=tick * 0.3))

    early = [frame(tick) for tick in range(3)]
    assert len(set(early)) > 1, "finishing really starts a short celebration"
    late_tick = int(FIREWORKS_SECONDS / 0.3) + 2
    calm = frame(late_tick)
    assert calm == frame(late_tick + 20), "finished scenes must stop repainting animation"
    assert any(value != calm for value in early), "early frames must include fireworks"
    assert "이번 주문을 모두 끝냈어요" in plain_frame(calm)
    for issues in snapshot.done.values():
        for issue in issues:
            assert f"#{issue.number}" in plain_frame(calm) and issue.title in plain_frame(calm)


def test_squirrel_done_animation_off_shows_finished_list_immediately_without_fireworks(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = done_snapshot()
    config = replace(config, animation=False)
    frames = [render_frame(snapshot, config, tmp_path, tick=tick, width=100)
              for tick in (0, 1, 2, 40)]
    assert len(set(frames)) == 1
    assert "▀" not in frames[0]
    assert "이번 주문을 모두 끝냈어요" in frames[0]
    assert all(f"#{issue.number}" in frames[0] for issues in snapshot.done.values() for issue in issues)

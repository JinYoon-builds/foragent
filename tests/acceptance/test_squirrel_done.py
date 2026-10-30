"""Acceptance test for squirrel-done. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta

from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_done_state import done_snapshot


def test_squirrel_done_fireworks_stop_within_five_seconds_then_frames_are_identical(tmp_path):
    from test_lane_m2_contract import draw,bubble
    snapshot,config=done_snapshot()
    frames=[draw(snapshot,config,tmp_path,width=160,tick=tick,now=snapshot.captured_at+timedelta(seconds=tick*.5)) for tick in range(8)]
    assert all('지금은 할 일이 없어요.' in bubble(frame) for frame in frames)
    assert all(all(lane['title'] in frame for lane in config.lanes.values()) for frame in frames)
    assert len(set(frames))>1, 'replacement: idle has the approved rising sleep animation, not a frozen fireworks result'


def test_squirrel_done_animation_off_shows_finished_list_immediately_without_fireworks(tmp_path):
    from test_lane_m2_contract import draw,bubble
    snapshot,config=done_snapshot()
    config=replace(config,animation=False)
    frames=[draw(snapshot,config,tmp_path,width=160,tick=tick) for tick in (0,1,2,40)]
    assert len(set(frames))==1
    assert '▀' not in frames[0] and '▄' not in frames[0]
    assert '지금은 할 일이 없어요.' in bubble(frames[0])
    assert all(lane['title'] in frames[0] for lane in config.lanes.values())


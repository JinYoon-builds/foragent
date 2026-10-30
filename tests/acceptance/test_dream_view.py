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
    from test_squirrel_running import plain_frame
    rows=[line for line in plain_frame(frame).splitlines() if config.lanes[lane]['title'] in line]
    assert len(rows)==1, 'each idle lane name occurs once in the shared sleeping block'
    return rows[0]


@pytest.mark.parametrize("width,lane_index", [(56, 0), (44, 1)])
def test_dream_view_active_dream_replaces_only_its_idle_lane_row(
    tmp_path, dream_view_offline, width, lane_index,
):
    from test_lane_m2_contract import draw
    snapshot,config,lane,store=dreaming_snapshot(tmp_path,lane_index)
    assert store.get_dream(lane).status=='running' and snapshot.status['active_runs']==[]
    frame=draw(snapshot,config,tmp_path,width=width)
    assert '💭' in lane_row(frame,config,lane), 'a dreaming lane is marked beside its name in the shared idle block'
    assert frame.count('💭')==1
    assert all(config.lanes[other]['title'] in frame for other in config.lanes)
    assert dream_view_offline==[]


@pytest.mark.parametrize("lane_index", [0, 1])
@pytest.mark.parametrize("language,words", [("ko", "꿈꾸는 중"), ("en", "dreaming")], ids=["ko", "en"])
def test_dream_view_text_mode_names_the_dreaming_lane_and_clears_on_completion(
    tmp_path, dream_view_offline, lane_index, language, words,
):
    from test_lane_m2_contract import draw
    snapshot,config,lane,store=dreaming_snapshot(tmp_path,lane_index,language=language)
    frame=draw(snapshot,config,tmp_path,width=43)
    assert '💭' in lane_row(frame,config,lane)
    store.finish_dream(lane,'dreamed')
    assert draw(snapshot,config,tmp_path,width=43)==frame, 'rendering uses captured dream facts'
    after=draw(dream_snapshot(store,config,snapshot.captured_at),config,tmp_path,width=43)
    assert '💭' not in after and config.lanes[lane]['title'] in after
    assert ('쉬고 있어요' if language=='ko' else 'resting') in after
    assert dream_view_offline==[]


def test_dream_view_sleep_without_an_active_dream_has_no_bubble(
    tmp_path, dream_view_offline,
):
    from test_lane_m2_contract import draw,bubble
    snapshot,config,lane,store=dreaming_snapshot(tmp_path,active=False)
    frame=draw(snapshot,config,tmp_path,width=160)
    assert '💭' not in frame and '지금은 할 일이 없어요.' in bubble(frame)
    store.start_dream(lane,worktree=tmp_path/'dream')
    store.finish_dream(lane,'dreamed')
    after=draw(dream_snapshot(store,config,snapshot.captured_at),config,tmp_path,width=160)
    assert '💭' not in after
    store.start_run(901,branch='foragent/issue-901',worktree=store.worktree_path(901),lane=lane)
    busy=draw(dream_snapshot(store,config,snapshot.captured_at),config,tmp_path,width=160)
    assert '💭' not in busy and '#901' in busy
    assert dream_view_offline==[]


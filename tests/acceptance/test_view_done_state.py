"""Acceptance test for view-done-state. Locked by Foragent; do not edit."""

import unicodedata

import pytest


def done_snapshot(*, language="ko", count=2, title="Current order"):
    from conftest import BASE_TIME
    from foragent.config import Config
    from foragent.labels import lane_display
    from foragent.tracker import FakeTracker
    from foragent.watch import Snapshot

    lanes = {f"lane-{i}": {"title": f"담당{i}" if language == "ko" else f"Agent{i}",
                           "emoji": "🧩", "paths": [f"area{i}/**"]} for i in range(count)}
    config = Config(language=language, lanes=lanes, skill_libraries=[])
    tracker = FakeTracker()
    tracker.add_issue(99, title)
    rows, done = {}, {}
    for index, slug in enumerate(lanes):
        rows[slug] = {"slug": slug, "lane_display": lane_display(config, slug, language),
                      "state": "idle", "issue": None, "title": None, "phase": None,
                      "elapsed_seconds": None, "last_activity_seconds": None,
                      "waiting_for": [], "running": [], "ready": [], "queued": []}
        done[slug] = []
        for offset in range(2 if index == 0 else 1):
            number = index * 10 + offset + 1
            tracker.add_issue(number, f"Completed work {number}", labels=[f"lane:{slug}"], state="closed")
            tracker.set_parent(number, 99)
            done[slug].append(tracker.get_issue(number))
    total = sum(map(len, done.values()))
    status = {"lanes": rows, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [], "history": []}
    return Snapshot(status, done, tracker.get_issue(99), total, total, None, [], BASE_TIME), config


def cell_count(value):
    return sum(0 if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}
               else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def test_view_done_state_completed_order_has_completion_sentence_and_each_issue_lane(tmp_path):
    from test_lane_m2_contract import draw,bubble
    snapshot,config=done_snapshot()
    snapshot.today=3
    snapshot.today_done={'lane-0':2,'lane-1':1}
    snapshot.status['issues']={}
    frame=draw(snapshot,config,tmp_path,width=160)
    assert '지금은 할 일이 없어요. 새 일은 기획 세션에서 말해 주세요.' in bubble(frame)
    assert '100% · 이슈 3개 중 3개 완료' in frame
    assert all(lane['title'] in frame for lane in config.lanes.values()), 'completion is a shared idle block, not one historical row per issue'


@pytest.mark.parametrize("language,done_words,header", [("ko", "2개 끝냄", "지금 하는 일"),
                                                       ("en", "2 done", "now doing")])
def test_view_done_state_idle_uses_words_and_omits_work_header_and_arrows(tmp_path, language, done_words, header):
    from test_lane_m2_contract import draw
    snapshot,config=done_snapshot(language=language)
    frame=draw(snapshot,config,tmp_path,width=160)
    idle='쉬고 있어요' if language=='ko' else 'resting'
    assert idle in frame and header not in frame
    assert '▸' not in frame and '▶' not in frame
    assert all(lane['title'] in frame for lane in config.lanes.values())
    assert any('담당0' in line and '담당1' in line for line in frame.splitlines()) if language=='ko' else any('Agent0' in line and 'Agent1' in line for line in frame.splitlines())


def test_view_done_state_waiting_count_is_shown_at_most_once(tmp_path):
    from foragent.i18n import text
    from foragent.watch import render_frame

    snapshot, config = done_snapshot()
    frame = render_frame(snapshot, config, tmp_path)
    assert frame.count(text("watch_waiting", "ko", count=0)) <= 1


def test_view_done_state_narrow_frame_keeps_every_lane_visible_with_long_order_title(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = done_snapshot(count=8, title="주" * 60)
    frame = render_frame(snapshot, config, tmp_path, width=80)
    assert all(cell_count(line) <= 80 for line in frame.splitlines())
    for lane in config.lanes.values():
        assert lane["title"] in frame, f"lane disappeared at width 80: {lane['title']}"


"""Acceptance test for lane-m2-controls. Locked by Foragent; do not edit."""

from dataclasses import replace
import pytest
from test_acorn_interact import hit_positions
from test_settings_window import settings_case, readable_settings
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, single_worker, view_offline, utf8_terminal
from test_view_done_state import cell_count


HINTS = {'ko':'도토리를 누르면 설명해요 · s 설정 · m 사용량 모드 · q 끝내기',
         'en':'Click an acorn to see what it is · s settings · m usage mode · q quit'}


@pytest.mark.parametrize('language,question,today,gear', [
    ('ko','답할 질문 1개','오늘 이슈 4개 머지','[⚙ 설정]'),
    ('en','1 question to answer','today 4 issues merged','[⚙ settings]'),
])
def test_top_line_keeps_r56_copy_count_and_a_text_settings_button_at_the_right(tmp_path, language, question, today, gear):
    from foragent.watch import render_frame, hit_test
    snapshot, config = single_worker()
    snapshot.today = 4
    snapshot.status['issues'] = {'needs-human':[{'number':201}]}
    snapshot.status['summary']['waiting_human'] = 1
    config = replace(config, language=language)
    for width in (160, 100):
        layout = {}
        line = plain_frame(render_frame(snapshot, config, tmp_path, width=width, layout=layout)).splitlines()[0]
        assert question in line and today in line and line.endswith(gear)
        assert cell_count(line) == width and 'Jin' not in line
        start = cell_count(line[:line.index(gear)])
        assert all(hit_test(layout, x, 0) == ('settings',None) for x in range(start,width)), 'whole gear text button is clickable'
    snapshot.status['issues']['needs-human'] = []
    snapshot.status['summary']['waiting_human'] = 0
    assert ('잘 돌고 있어요' if language == 'ko' else 'running fine') in plain_frame(render_frame(snapshot,config,tmp_path,width=160)).splitlines()[0]
    snapshot.status['active_runs'] = []
    snapshot.status['lanes']['work'].update(state='idle', issue=None, running=[])
    snapshot.status['summary']['working'] = 0
    assert ('쉬고 있어요' if language == 'ko' else 'resting') in plain_frame(render_frame(snapshot,config,tmp_path,width=160)).splitlines()[0]


@pytest.mark.parametrize('language', ['ko','en'])
def test_button_and_s_open_the_same_settings_window_over_the_new_layout(git_history,tmp_path,monkeypatch,language):
    from foragent.watch import render_frame,handle_mouse,handle_key
    from foragent.config import load_config
    case, _ = settings_case(git_history,tmp_path,monkeypatch)
    readable_settings(case)
    config = replace(load_config(case.root),language=language)
    snapshot,_ = single_worker()
    state,layout = {'store':case.store},{}
    frame = render_frame(snapshot,config,case.root,width=160,state=state,layout=layout)
    hits = hit_positions(layout,frame,160)
    assert ('settings',None) in hits, 'addendum 6 requires a text settings button target'
    handle_mouse('left',*hits[('settings',None)],layout,case.store,config,state=state)
    assert state.get('settings') is True
    clicked = plain_frame(render_frame(snapshot,config,case.root,width=160,state=state))
    keyboard = {'store':case.store}
    assert handle_key('s',case.store,config,state=keyboard,repo_root=case.root) is False
    opened = plain_frame(render_frame(snapshot,config,case.root,width=160,state=keyboard))
    assert clicked == opened, 'button and s must open the same existing settings window'
    assert '━' * 12 in opened and 'Jin' not in opened, 'settings inherit the redesigned separators and copy'
    for label in (('팀','나','이 컴퓨터') if language == 'ko' else ('Team','Me','This computer')):
        assert label.casefold() in opened.casefold()
    assert 'Claude' in opened.splitlines()[-2] and 'Codex' in opened.splitlines()[-2]
    assert opened.splitlines()[-1] == HINTS[language]
    assert handle_key('esc',case.store,config,state=state,repo_root=case.root) is False
    closed = plain_frame(render_frame(snapshot,config,case.root,width=160,state=state))
    assert closed == plain_frame(frame), 'Esc restores the lane layout'


@pytest.mark.parametrize('language',['ko','en'])
def test_usage_percentages_and_mode_stay_above_the_hints_and_m_still_toggles(git_history,tmp_path,monkeypatch,capsys,language):
    from foragent.watch import render_frame, handle_key
    from test_usage_read import usage_case
    from test_view_usage_line import usage_snapshot
    case=usage_case(git_history,tmp_path,monkeypatch,used_percent=6)
    case.ctx.config=replace(case.ctx.config,language=language,max_parallel=17)
    snapshot,_=usage_snapshot(case,monkeypatch,capsys)
    state={'store':case.store}
    for expected in ('keep','burn','keep'):
        lines=plain_frame(render_frame(snapshot,case.ctx.config,case.root,width=240,state=state)).splitlines()
        assert lines[-1] == HINTS[language], 'addendum 5 puts hints after usage'
        assert all(value in lines[-2] for value in ('Claude','6%','Codex','67%',expected))
        assert '17' not in lines[-2] and '동시' not in lines[-2] and 'running ' not in lines[-2]
        if expected != 'keep' or 'quota_mode' not in state:
            handle_key('m',case.store,case.ctx.config,state=state,repo_root=case.root)


def test_m_keeps_working_while_the_settings_window_is_open(git_history,tmp_path,monkeypatch):
    from foragent.watch import render_frame,handle_key
    case,_=settings_case(git_history,tmp_path,monkeypatch)
    readable_settings(case)
    snapshot,config=single_worker()
    state={'store':case.store}
    handle_key('s',case.store,config,state=state,repo_root=case.root)
    for mode in ('burn','keep'):
        handle_key('m',case.store,config,state=state,repo_root=case.root)
        lines=plain_frame(render_frame(snapshot,config,case.root,width=240,state=state)).splitlines()
        assert state['settings'] is True and mode in lines[-2]
        assert lines[-1] == HINTS['ko']


@pytest.mark.parametrize('language,zero', [('ko','오늘 이슈 0개 머지'),('en','today 0 issues merged')])
def test_zero_today_header_matches_the_prototype(tmp_path, language, zero):
    from foragent.watch import render_frame
    snapshot, config = single_worker()
    snapshot.today = 0
    first = plain_frame(render_frame(snapshot,replace(config,language=language),tmp_path,width=160)).splitlines()[0]
    assert zero in first

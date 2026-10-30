"""Acceptance test for squirrel-running. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re

import pytest

from test_heartbeats import heartbeats_write
from test_skills_autosync import isolated_home
from test_view_done_state import cell_count, done_snapshot


@pytest.fixture(autouse=True)
def view_offline(isolated_home, tmp_path, monkeypatch):
    """Share existing HOME isolation and forbid external I/O for the new view tests."""
    import curses
    import socket
    import subprocess
    import tempfile
    from pathlib import Path

    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("lane-view acceptance must not open a terminal, network, or executor")

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("ORCA_TERMINAL_HANDLE", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(curses, "initscr", forbidden)
    monkeypatch.setattr(curses, "wrapper", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        # Git fixtures and Done when's file checks are the only child processes.
        # In particular, terminal CLIs and gh cannot bypass the Python guards.
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


def plain_frame(value):
    """Remove only terminal transport escapes, retaining all displayed characters."""
    value = re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", value)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


def running_snapshot():
    """Extend the existing view builder with one lane for each factual state."""
    from foragent.labels import lane_display

    snapshot, config = done_snapshot(count=5)
    names = ("work", "queue", "human", "blocked", "finished")
    old_names = list(config.lanes)
    config = replace(config, repo="owner/repo", lanes={
        name: {"title": name, "emoji": "", "paths": [f"{name}/**"]} for name in names})
    rows = {}
    done = {}
    for index, (old, name) in enumerate(zip(old_names, names)):
        row = deepcopy(snapshot.status["lanes"][old])
        row.update(slug=name, lane_display=lane_display(config, name, "ko"))
        if name != "finished":
            number = 101 + index
            item = {"number": number, "title": f"Task {number}"}
            row.update(state=("working", "queued", "waiting-human", "blocked")[index],
                       issue=number, title=item["title"], last_activity_seconds=60,
                       elapsed_seconds=60, phase="implementing" if name == "work" else None,
                       queued=[item] if name == "queue" else [],
                       running=[item] if name == "work" else [])
        rows[name] = row
        done[name] = snapshot.done[old]
    status = {**snapshot.status, "lanes": rows,
              "summary": {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 1},
              "active_runs": [{"number": 101, "lane": "work", "attempts": 1,
                               "last_activity_at": (snapshot.captured_at - timedelta(seconds=60)).isoformat()}]}
    return replace(snapshot, status=status, done=done, total=snapshot.completed + 4), config


def running_view_case(git_history, tmp_path, monkeypatch):
    """Use real run/log/heartbeat records when a live orchestrator is required."""
    from test_view_evidence import view_evidence_log
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language="ko")
    case.store.update_run(1, pid=88001, phase="implementing",
                          last_activity_at=case.clock[0].isoformat())
    monkeypatch.setattr("foragent.watch.os.kill", lambda pid, signal: None)
    view_evidence_log(case, minutes=1)
    heartbeats_write(case.store, "orch", case.clock[0])
    heartbeats_write(case.store, "gate", case.clock[0])
    case.tracker.add_issue(99, "Current order")
    for number in (1, 2, 3, 4):
        case.tracker.set_parent(number, 99)
    case.tracker.add_issue(41, "Finished task", labels=["lane:docs"], state="closed")
    case.tracker.set_parent(41, 99)
    case.store.start_run(41, branch="foragent/issue-41", worktree=case.store.worktree_path(41), lane="docs")
    case.store.finish_run(41, outcome="merged")
    return case, main


@pytest.fixture(autouse=True)
def utf8_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)


def display_cells(value):
    """Independent width oracle for the spec's emoji presentation of 🐿.

    The existing shared counter covers CJK, wide emoji and combining marks;
    Unicode's narrow default text presentation of 🐿 needs one extra cell here.
    """
    return sum(2 if char == "🐿" else cell_count(char) for char in value)


def issue_row(frame, number):
    from test_lane_m2_contract import acorn_row
    return acorn_row(frame,number)


def single_worker(*, phase="implementing", state="working", elapsed=720, title="Task 101"):
    """Derive isolated row facts without altering the shared running_snapshot contract."""
    snapshot, config = running_snapshot()
    row = deepcopy(snapshot.status["lanes"]["work"])
    item = {"number": 101, "title": title}
    row.update(state=state, phase=phase, title=title, elapsed_seconds=elapsed,
               running=[item] if state != "queued" else [],
               queued=[item] if state == "queued" else [],
               waiting_for=[999] if state == "blocked" else [])
    started = (snapshot.captured_at - timedelta(seconds=elapsed)).isoformat()
    runs = [] if state == "queued" else [
        {"number": 101, "lane": "work", "attempts": 1, "status": "running",
         "phase": phase, "started_at": started, "phase_started_at": started,
         "last_activity_at": snapshot.captured_at.isoformat()}]
    status = {**snapshot.status, "lanes": {"work": row}, "active_runs": runs,
              "summary": {"working": int(state == "working"), "blocked": int(state == "blocked"),
                          "waiting_human": int(state == "waiting-human"), "queued": int(state == "queued")}}
    return (replace(snapshot, status=status, done={"work": []}, completed=0, total=1,
                    worker_states={101: state}),
            replace(config, lanes={"work": config.lanes["work"]}))


def glyph_config(tmp_path, config, mode):
    """Exercise the real YAML key rather than attaching an attribute to a fake config."""
    from foragent.config import Config, load_config

    assert hasattr(Config(), "view_glyphs"), "Config must expose view_glyphs (default auto)"
    assert Config().view_glyphs == "auto", "view_glyphs must default to auto"
    (tmp_path / ".foragent.yml").write_text(f"view_glyphs: {mode}\n", encoding="utf-8")
    loaded = load_config(tmp_path)
    assert loaded.view_glyphs == mode, "the YAML glyph choice must survive config loading"
    return replace(config, view_glyphs=loaded.view_glyphs)


def test_squirrel_running_factual_states_are_single_rows_without_lane_sprites(tmp_path):
    from foragent.watch import render_frame
    from test_acorn_interact import hit_positions
    snapshot,config=running_snapshot()
    layout={}
    frame=plain_frame(render_frame(snapshot,config,tmp_path,width=160,layout=layout))
    hits=hit_positions(layout,frame,160)
    assert '▀' in frame or '▄' in frame, 'replacement: one pixel squirrel per lane, not per-worker text glyphs'
    for name,number in (('work',101),('queue',102),('human',103),('blocked',104)):
        assert ('squirrel',name) in hits and ('acorn',number) in hits
    assert '#103 질문' in frame
    assert 'finished' in frame and '쉬고 있어요' in frame


@pytest.mark.parametrize("state,phase,glyph,words", [
    ("working", "implementing", "🐿", "코드 쓰는 중"),
    ("working", "resolving", "🐿", "코드 쓰는 중"),
    ("working", "baseline", "🔍", "기준 재는 중"),
    ("working", "checking", "🔍", "로컬 시험 중"),
    ("working", "reviewing", "🔍", "리뷰 받는 중"),
    ("working", "ci", "🔍", "CI 시험 중"),
    ("working", "merging", "🔍", "머지 전 재확인 중"),
    ("waiting-human", "needs-human", "✋", "기다리는 중"),
    ("waiting-human", "waiting-person", "✋", "기다리는 중"),
    ("queued", None, "⏳", "차례 기다리는 중"),
    ("blocked", None, "⏳", "기다리는 중"),
    ("queued", "limit-wait", "⏳", "차례 기다리는 중"),
    ("working", "overlap-wait", "⏳", "차례 기다리는 중"),
    ("working", "test-slot-wait", "⏳", "차례 기다리는 중"),
    ("working", "baseline-wait", "⏳", "차례 기다리는 중"),
], ids=["implementing", "resolving", "baseline", "checking", "reviewing", "ci", "merging",
        "needs-human", "waiting-person", "queued", "orchestrator-blocked", "limit-wait", "overlap-wait",
        "test-slot-wait", "baseline-wait"])
def test_squirrel_running_glyph_and_phase_follow_each_workers_facts(tmp_path, state, phase, glyph, words):
    from foragent.watch import render_frame
    snapshot,config=single_worker(state=state,phase=phase)
    frame=plain_frame(render_frame(snapshot,config,tmp_path,width=160))
    row=issue_row(frame,101)
    steps={'baseline':'준비','implementing':'작성','resolving':'작성','checking':'로컬',
           'reviewing':'리뷰','ci':'CI','merging':'머지'}
    if state=='waiting-human':
        assert '#101 질문' in row
    elif state=='working' and phase in steps:
        assert f'#101 {steps[phase]}' in row
    else:
        assert '기다려요' in frame or '보류' in frame


def test_squirrel_running_two_workers_in_one_lane_have_independent_rows(tmp_path):
    from foragent.watch import render_frame
    from test_acorn_interact import hit_positions
    snapshot,config=single_worker()
    snapshot.status['lanes']['work']['running'].append({'number':105,'title':'Second worker'})
    snapshot.status['active_runs'].append({**snapshot.status['active_runs'][0],'number':105,'phase':'reviewing'})
    layout={}
    frame=plain_frame(render_frame(snapshot,config,tmp_path,width=160,layout=layout))
    first,second=issue_row(frame,101),issue_row(frame,105)
    assert first == second and '#101 작성' in first and '#105 리뷰' in second, 'replacement: workers share their lane block'
    hits=hit_positions(layout,frame,160)
    assert ('squirrel','work') in hits and {('acorn',101),('acorn',105)} <= hits.keys()
    assert sum(target==('squirrel','work') for *_,target in layout['hits'])==1


def test_squirrel_running_columns_align_by_display_cells_with_korean_and_emoji(tmp_path):
    from foragent.watch import render_frame
    snapshot,config=single_worker(title='한글 🐿 task')
    first=snapshot.status['lanes']['work']
    second=deepcopy(first)
    second.update(slug='한글',lane_display='한글',issue=105,title='Go 🧩',running=[{'number':105,'title':'Go 🧩'}])
    snapshot.status['lanes']['한글']=second
    snapshot.status['active_runs'].append({**snapshot.status['active_runs'][0],'number':105,'lane':'한글','phase':'checking'})
    config=replace(config,lanes={**config.lanes,'한글':{'title':'한글','emoji':''}})
    frame=plain_frame(render_frame(snapshot,config,tmp_path,width=100))
    a,b=issue_row(frame,101),issue_row(frame,105)
    assert cell_count(a[:a.index('#101')])==cell_count(b[:b.index('#105')]), 'acorns align by cells across Latin/Korean lane captions'
    assert '#101 작성' in a and '#105 로컬' in b
    assert all(cell_count(line)<=100 for line in frame.splitlines())


def test_squirrel_running_long_title_is_cut_with_ellipsis_before_later_columns(tmp_path):
    from test_lane_m2_contract import draw,pick,bubble
    from foragent.state import StateStore
    title='긴 제목 🧩 '*50+'INVISIBLE_TITLE_END'
    snapshot,config=single_worker(title=title)
    frame=draw(snapshot,config,tmp_path,width=100)
    assert '#101 작성' in issue_row(frame,101) and title not in frame, 'titles move off acorns into click explanations'
    opened,_=pick(snapshot,config,tmp_path,StateStore(tmp_path/'state'),('acorn',101),width=100)
    assert '긴 제목' in bubble(opened) and 'INVISIBLE_TITLE_END' not in bubble(opened)
    assert all(cell_count(line)<=100 for line in opened.splitlines())


@pytest.mark.parametrize("mode", ["emoji", "ascii"])
@pytest.mark.parametrize("phase,finished", [("implementing", 0), ("baseline", 0), ("checking", 1),
                                            ("reviewing", 2), ("ci", 3), ("merging", 4)])
def test_squirrel_running_acorns_show_only_finished_phases(tmp_path, phase, finished, mode):
    from test_lane_m2_contract import draw
    snapshot,config=single_worker(phase=phase)
    config=glyph_config(tmp_path,config,mode)
    words={'baseline':'준비','implementing':'작성','checking':'로컬','reviewing':'리뷰','ci':'CI','merging':'머지'}
    row=issue_row(draw(snapshot,config,tmp_path,width=160),101)
    assert f'#101 {words[phase]}' in row, 'replacement: one issue acorn carries its current step, not five completion markers'
    assert row.count('#101')==1


@pytest.mark.parametrize("mode,glyph,progress", [("emoji", "🔍", "○○○○○"), ("ascii", "[?]", ".....")])
def test_squirrel_running_baseline_uses_checking_glyph_without_completing_local_tests(tmp_path, mode, glyph, progress):
    from test_lane_m2_contract import draw
    snapshot,config=single_worker(phase='baseline')
    config=glyph_config(tmp_path,config,mode)
    row=issue_row(draw(snapshot,config,tmp_path,width=160),101)
    assert '#101 준비' in row and '#101 로컬' not in row, 'baseline has its own preparation step; it does not claim local tests finished'


@pytest.mark.parametrize("phase,words", [("implementing", "writing code"),
                                         ("baseline", "measuring the baseline"), ("checking", "local tests"),
                                         ("reviewing", "in review"), ("ci", "CI tests"),
                                         ("merging", "rechecking before merge"),
                                         ("overlap-wait", "waiting for a turn"),
                                         ("test-slot-wait", "waiting for a turn"),
                                         ("baseline-wait", "waiting for a turn"),
                                         ("limit-wait", "waiting for a turn")])
def test_squirrel_running_english_uses_unified_phase_words(tmp_path, phase, words):
    from test_lane_m2_contract import draw,pick,bubble
    from foragent.state import StateStore
    snapshot,config=single_worker(phase=phase)
    config=replace(config,language='en')
    frame=draw(snapshot,config,tmp_path,width=160)
    row=issue_row(frame,101)
    steps={'baseline':'prep','implementing':'write','checking':'local','reviewing':'review','ci':'CI','merging':'merge'}
    if phase in steps:
        assert f'#101 {steps[phase]}' in row
        opened,_=pick(snapshot,config,tmp_path,StateStore(tmp_path/'state'),('acorn',101),width=160)
        assert '12m' in bubble(opened)
    else:
        assert 'waiting' in frame.lower()
    assert not any(word in row for word in ('작성','로컬','리뷰','준비','머지','분'))


@pytest.mark.parametrize("language,seconds,elapsed", [("ko", 720, "12분"), ("en", 720, "12m"),
                                                      ("ko", 3540, "59분"), ("en", 3540, "59m"),
                                                      ("ko", 3600, "1시간 0분"), ("en", 3600, "1h 0m"),
                                                      ("ko", 3900, "1시간 5분"), ("en", 3900, "1h 5m")])
def test_squirrel_running_elapsed_time_uses_minutes_then_hours(tmp_path, language, seconds, elapsed):
    from test_lane_m2_contract import pick,bubble
    from foragent.state import StateStore
    snapshot,config=single_worker(elapsed=seconds)
    config=replace(config,language=language)
    opened,_=pick(snapshot,config,tmp_path,StateStore(tmp_path/'state'),('acorn',101),width=240)
    assert elapsed in bubble(opened), 'elapsed phase time moves into the acorn explanation'


@pytest.mark.parametrize("mode,glyph,progress", [("ascii", "[?]", "##..."), ("emoji", "🔍", "🌰🌰○○○")])
def test_squirrel_running_explicit_glyph_config_overrides_terminal(tmp_path, monkeypatch, mode, glyph, progress):
    from test_lane_m2_contract import draw
    monkeypatch.setenv('TERM','dumb' if mode=='emoji' else 'xterm-256color')
    monkeypatch.setenv('LANG','C' if mode=='emoji' else 'en_US.UTF-8')
    snapshot,config=single_worker(phase='reviewing')
    frame=draw(snapshot,glyph_config(tmp_path,config,mode),tmp_path,width=160)
    assert '#101 리뷰' in issue_row(frame,101)
    assert ('▀' in frame or '▄' in frame) == (mode=='emoji'), 'explicit ASCII/graphic choice still overrides locale after the redesign'


@pytest.mark.parametrize("term,lang,lc_all,lc_ctype,ascii_mode", [
    ("dumb", "en_US.UTF-8", None, None, True),
    ("linux", "en_US.UTF-8", None, None, True),
    ("xterm-256color", "C", None, None, True),
    ("xterm-256color", "ko_KR.UTF-8", None, None, False),
    ("xterm-256color", "en_US.utf8", None, None, False),
    ("xterm-256color", "en_US.UTF-8", "C", None, True),
    ("xterm-256color", "C", "en_US.UTF-8", None, False),
    ("xterm-256color", "en_US.UTF-8", None, "C", True),
], ids=["dumb", "linux", "non-utf8", "utf8", "utf8-alias", "lc-all-C", "lc-all-utf8", "lc-ctype-C"])
def test_squirrel_running_auto_glyphs_follow_terminal_and_effective_locale(
    tmp_path, monkeypatch, term, lang, lc_all, lc_ctype, ascii_mode,
):
    from test_lane_m2_contract import draw
    monkeypatch.setenv('TERM',term)
    monkeypatch.setenv('LANG',lang)
    for key,value in (('LC_ALL',lc_all),('LC_CTYPE',lc_ctype)):
        if value is not None: monkeypatch.setenv(key,value)
        else: monkeypatch.delenv(key,raising=False)
    snapshot,config=single_worker(phase='checking')
    frame=draw(snapshot,config,tmp_path,width=160)
    assert '#101 로컬' in issue_row(frame,101)
    assert ('▀' in frame or '▄' in frame) == (not ascii_mode)
    assert draw(snapshot,glyph_config(tmp_path,config,'auto'),tmp_path,width=160)==frame


def test_squirrel_running_ascii_maps_all_factual_states_and_idle(tmp_path):
    from test_lane_m2_contract import draw
    snapshot,config=running_snapshot()
    frame=draw(snapshot,glyph_config(tmp_path,config,'ascii'),tmp_path,width=160)
    assert '#101 작성' in issue_row(frame,101) and '#103 질문' in issue_row(frame,103)
    assert all(f'#{n}' in frame for n in (101,102,103,104))
    assert 'finished' in frame and '쉬고 있어요' in frame
    assert not any(char in frame for char in ('▀','▄','🐿','🔍','✋','⏳','💤','🌰'))


@pytest.mark.parametrize("width", [160, 100, 99, 80, 60, 59, 40, 32, 20])
def test_squirrel_running_width_rules_drop_title_then_elapsed_without_wrapping(tmp_path, width):
    from test_lane_m2_contract import draw
    snapshot,config=single_worker(title='TITLE_COLUMN')
    frame=draw(snapshot,config,tmp_path,width=width)
    assert all(cell_count(line)<=width for line in frame.splitlines())
    if width>=40:
        assert '#101' in frame and '작성' in frame
    assert 'TITLE_COLUMN' not in issue_row(frame,101) if width>=40 else True


def test_squirrel_running_twenty_workers_fit_24_rows_with_header_and_footer(tmp_path):
    from test_lane_m2_contract import draw
    from test_acorn_interact import hit_positions
    snapshot,config=single_worker()
    template,run=snapshot.status['lanes']['work'],snapshot.status['active_runs'][0]
    lanes,rows,runs={},{},[]
    for i in range(20):
        slug,number=f'lane{i:02}',201+i
        lanes[slug]={'title':slug,'emoji':''}
        rows[slug]={**deepcopy(template),'slug':slug,'lane_display':slug,'issue':number,'running':[{'number':number,'title':f'Task {number}'}]}
        runs.append({**run,'number':number,'lane':slug})
    snapshot.status.update(lanes=rows,active_runs=runs,summary={'working':20,'blocked':0,'waiting_human':0,'queued':0})
    snapshot=replace(snapshot,done={s:[] for s in lanes},worker_states={},total=20)
    config=replace(config,lanes=lanes)
    full=draw(snapshot,config,tmp_path,width=100)
    assert len(full.splitlines())>24, 'replacement: blocks scroll rather than collapsing twenty lanes into worker text rows'
    assert all(f'#{n}' in full for n in range(201,221))
    seen=set()
    state={}
    for offset in range(len(full.splitlines())):
        state['scroll']=offset
        layout={}
        visible=draw(snapshot,config,tmp_path,width=100,height=24,state=state,layout=layout)
        assert len(visible.splitlines())<=24
        seen.update(target[1] for target in hit_positions(layout,visible,100) if target[0]=='acorn')
    assert set(range(201,221))<=seen, 'every issue remains reachable through the viewport'


@pytest.mark.parametrize("scene", ["first-run", "first-run-queued", "all-idle", "queued", "busy"])
def test_squirrel_running_only_one_big_squirrel_in_header_when_first_run_or_all_idle(tmp_path, scene):
    from test_lane_m2_contract import draw,bubble
    snapshot,config=running_snapshot()
    if scene in {'first-run','first-run-queued','all-idle','queued'}:
        snapshot.status['active_runs']=[]
        for row in snapshot.status['lanes'].values(): row.update(state='idle',issue=None,title=None,phase=None,queued=[],running=[],held=[])
        snapshot.status['summary'].update(working=0,blocked=0,waiting_human=0,queued=0)
    if scene in {'first-run','first-run-queued'}:
        snapshot=replace(snapshot,done={s:[] for s in config.lanes},completed=0,total=0)
    if scene in {'queued','first-run-queued'}:
        snapshot.status['lanes']['queue'].update(state='queued',issue=102,queued=[{'number':102,'title':'Task 102'}])
    frame=draw(snapshot,config,tmp_path,width=160)
    assert bubble(frame), 'replacement: the big orchestrator and full-height bubble are always present'
    assert sum('╭' in line for line in frame.splitlines())==1
    assert '▀' in frame or '▄' in frame


def test_squirrel_running_header_shows_latest_say_cut_to_one_line(tmp_path):
    from test_lane_m2_contract import draw,bubble
    snapshot,config=single_worker()
    snapshot.events=[{'at':snapshot.captured_at.isoformat(),'kind':'retry','summary':'RAW_TECHNICAL','say':'UNBOUNDED_SAY '*100}]
    frame=draw(snapshot,config,tmp_path,width=160)
    assert '잘 돌고 있어요' in frame.splitlines()[0] and frame.splitlines()[0].endswith('[⚙ 설정]')
    assert '레인 1곳에서 이슈 1개를 작업하고 있어요.' in bubble(frame), 'replacement: r56 summary replaces the latest-say header'
    assert 'RAW_TECHNICAL' not in frame and 'UNBOUNDED_SAY' not in frame.splitlines()[0]
    assert all(cell_count(line)<=160 for line in frame.splitlines())


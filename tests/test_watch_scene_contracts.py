"""Scene regressions: decorative glyphs, authored selection and shared elision."""
from datetime import timedelta

import pytest

from foragent import watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.watch_sections import lane_blocks, lane_bubble
from test_watch import NOW
from test_watch_scene_review import _snapshot


def row(slug='work'):
    return {'slug': slug, 'lane_display': slug, 'state': 'queued', 'issue': 2,
            'running': [], 'held': [], 'queued': [{'number': 2, 'title': 'Choice'}]}


@pytest.mark.parametrize('language', ['en', 'ko'])
@pytest.mark.parametrize('mode', ['ascii', 'non-utf8'])
def test_scene_decorations_follow_ascii_mode_without_pixels(language, mode, tmp_path, monkeypatch):
    from foragent import watch_scene

    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('LC_ALL', 'C' if mode == 'non-utf8' else 'C.UTF-8')
    config = Config(language=language, view_glyphs='auto' if mode == 'non-utf8' else 'ascii',
                    lanes={'work': {'paths': ['src/']}, 'rest-a': {}, 'rest-b': {}})
    work = row()
    snapshot = _snapshot(work, today=1, today_done={'rest-a': 1},
                         waiting_facts={2: {'kind': 'capacity'}})
    for slug in ('rest-a', 'rest-b'):
        snapshot.status['lanes'][slug] = {**row(slug), 'state': 'idle', 'issue': None, 'queued': []}
    snapshot.status['issues'] = {'ready': [{'number': 2}]}
    snapshot.requests = [{'status': 'processing', 'text': 'Request'}, {'status': 'done'}]
    snapshot.panel = {'inbox': 1}
    snapshot.heartbeats = {'orch': 900}
    for selected in (None, ('acorn', 2)):
        state = {} if selected is None else {'explanation_target': selected}
        lines = lane_bubble.render(snapshot, config, tmp_path, width=120, now=NOW, state=state)
        lines += lane_blocks.render(snapshot, config, tmp_path, width=120, now=NOW)
        value = '\n'.join(''.join(part for part, _ in line) for line in lines)
        assert not set(value) & set('█░·○⚠—…▀▄━✓'), value
        assert '50% |' in value and '#' * 10 in value
        assert 'rest-a v1 | rest-b' in value
        if language == 'en':
            assert value.isascii(), value
    snapshot.status['issues'] = {}
    one = ''.join(value for line in lane_bubble._bar(snapshot, language, 120, '...', ascii=True)
                  for value, _ in line)
    assert '100% |' in one and '·' not in one
    monkeypatch.setattr(watch, '_activity', lambda *args: '○ looks stalled 10 min')
    item = {'number': 2, 'state': 'working', 'phase': 'implementing',
            'run': {'started_at': NOW.isoformat()}}
    assert watch_scene.activity_warning(snapshot, config, work, item, NOW) == 'o looks stalled 10 min'


def test_ascii_templates_preserve_literal_middle_dots_in_names_paths_and_reasons():
    from foragent import watch_scene

    assert watch_scene.text('lane_bubble_lane_paths', 'en', ascii=True, lane='literal·lane',
                            paths='src/·file.py') == 'lane literal·lane | src/·file.py'
    work = row()
    snapshot = _snapshot(work, meter={'running': 1, 'budget': 1}, waiting_facts={2: {
        'kind': 'file-overlap', 'overlapping_files': ['src/·file.py']}})
    assert watch_scene.waiting_reason(snapshot, work, 2, 'en', ascii=True) == (
        'waiting its turn | file overlap: src/·file.py | slots full')


@pytest.mark.parametrize('language,labels', [
    ('en', ('Waiting:', 'Next:', 'Owner:')), ('ko', ('기다리는 이유:', '다음 조건:', '담당 역할:'))])
@pytest.mark.parametrize('state', ['facts-only', 'queued', 'working', 'blocked', 'waiting-human', 'parked'])
def test_selecting_an_authored_wait_shows_its_canonical_context_not_a_guessed_turn(
    language, labels, state, tmp_path,
):
    work = row()
    if state == 'facts-only':
        work.update(state='idle', issue=None, queued=[])
    elif state in {'working', 'blocked', 'waiting-human'}:
        work.update(state=state, queued=[])
        work['running' if state == 'working' else 'held'] = [{'number': 2, 'title': 'Choice', 'state': state}]
    snapshot = _snapshot(work, waiting_facts={2: {'lane': 'work', 'title': 'Choice', 'authored': {
        'text': 'Original wait', 'reason': 'Locale  choice pending', 'next_condition': 'Record locale',
        'responsible_role': 'planner'}}})
    snapshot.status.update(summary={'working': 0, 'blocked': 0, 'waiting_human': 0, 'queued': 0}, history=[])
    if state == 'parked':
        snapshot.park_facts[2] = {'parked': True}
    config = Config(language=language, view_glyphs='ascii', lanes={'work': {}})
    layout, view = {}, {}
    watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state=view, layout=layout)
    block = '\n'.join(''.join(value for value, _ in line) for line in
                      lane_blocks.render(snapshot, config, tmp_path, width=120, now=NOW))
    x, y, *_, target = next(hit for hit in layout['hits'] if hit[-1] == ('acorn', 2))
    watch.handle_mouse('left', x, y, layout, StateStore(tmp_path / 'state'), config, state=view)
    assert view['explanation_target'] == target
    frame = watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state=view)
    lines = frame.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith('+'))
    body = '\n'.join(lines[top:top + 8])
    for label, value in zip(labels, ('Locale  choice pending', 'Record locale', 'planner')):
        assert block.count(f'{label} {value}') == 1, block
        if state in {'facts-only', 'queued'}:
            assert body.count(f'{label} {value}') == 1, body
    primary = {
        'working': ('working', '일하고 있어요'),
        'blocked': ('handled by the orchestrator', '오케스트레이터가 보는 중'),
        'waiting-human': ('I will ask in the planning session.', '기획 세션에서 물어볼게요.'),
        'parked': ('Parked in lane work.', 'work 레인에서 보류 중이에요.'),
    }
    if state in primary:
        assert primary[state][language == 'ko'] in body, body
    if state == 'blocked':
        assert ('순서를 기다려요' if language == 'ko' else 'waiting its turn') in block, block
    for value in ('waiting its turn', 'Waiting its turn', '순서를 기다려요'):
        assert value not in body
        if state != 'blocked':
            assert value not in block


@pytest.mark.parametrize('language', ['en', 'ko'])
@pytest.mark.parametrize('phase,english,korean', [
    ('implementing', 'writing code', '코드를 쓰고 있어요'),
    ('baseline', 'checking that the acceptance tests fail now', '완료 테스트가 지금 실패하는지 확인하고 있어요'),
    ('checking', 'running related tests on this computer', '내 컴퓨터에서 관련 테스트를 돌리고 있어요'),
    ('reviewing', 'another AI is reviewing', '다른 AI가 리뷰하고 있어요'),
    ('ci', 'running CI on GitHub', 'GitHub에서 CI를 돌리고 있어요'),
    ('merging', 'merging into main', 'main에 머지하고 있어요'),
    ('overlap-wait', 'waiting for shared files', '공유 파일 기다려요'),
    ('test-slot-wait', 'waiting for a test slot', '시험 자리 기다리는 중'),
    ('baseline-wait', 'waiting for the baseline', '기준선 기다려요'),
    ('limit-wait', 'waiting for the usage limit', '사용량 한도 기다려요'),
    ('executor-wait', 'waiting to retry the model', '모델 재시도 기다려요'),
])
def test_authored_context_keeps_the_recorded_phase_time_and_warning(
    language, phase, english, korean, tmp_path,
):
    work = row()
    work.update(state='working', phase=phase, running=work['queued'], queued=[])
    snapshot = _snapshot(work, log_ages={2: 900}, waiting_facts={2: {
        'lane': 'work', 'kind': 'capacity', 'authored': {
            'text': 'Original wait', 'reason': 'Locale pending',
            'next_condition': 'Record locale', 'responsible_role': 'planner'}}})
    snapshot.status['active_runs'] = [{
        'number': 2, 'lane': 'work', 'phase': phase,
        'started_at': (NOW - timedelta(minutes=30)).isoformat(),
        'phase_started_at': (NOW - timedelta(minutes=12)).isoformat(),
        'last_activity_at': (NOW - timedelta(minutes=15)).isoformat(),
    }]
    config = Config(language=language, view_glyphs='ascii', lanes={'work': {}})
    body = '\n'.join(''.join(value for value, _ in line) for line in lane_bubble.render(
        snapshot, config, tmp_path, width=120, now=NOW, state={'explanation_target': ('acorn', 2)}))
    expected = ['#2 Choice', 'work 레인' if language == 'ko' else 'lane work',
                korean if language == 'ko' else english,
                '이 단계를 12분째 하고 있어요.' if language == 'ko' else '12m in this step',
                '? 15분째 조용해요' if language == 'ko' else '? quiet for 15 min']
    positions = [body.index(value) for value in expected]
    assert positions == sorted(positions), body
    block = '\n'.join(''.join(value for value, _ in line) for line in lane_blocks.render(
        snapshot, config, tmp_path, width=120, now=NOW))
    for value in ('Locale pending', 'Record locale', 'planner'):
        assert block.count(value) == 1, block
    if phase.endswith('-wait'):
        assert ('순서를 기다려요 | 자리가 다 찼어요' if language == 'ko'
                else 'waiting its turn | slots full') in block, block


@pytest.mark.parametrize('language', ['en', 'ko'])
@pytest.mark.parametrize('evidence', ['listing', 'needs-human', 'representative', 'held-duplicate', 'parked'])
def test_authored_context_keeps_question_and_park_priority_over_an_active_wait(
    language, evidence, tmp_path,
):
    work = row()
    work.update(state='working', phase='test-slot-wait', running=work['queued'], queued=[])
    snapshot = _snapshot(work, park_facts={2: {'parked': True}}, waiting_facts={2: {
        'lane': 'work', 'authored': {'text': 'Canonical waiting context'}}})
    if evidence == 'listing':
        work['running'][0]['state'] = 'waiting-human'
    elif evidence == 'needs-human':
        snapshot.status['issues'] = {'needs-human': [{'number': 2}]}
    elif evidence == 'representative':
        work['state'] = 'waiting-human'
    elif evidence == 'held-duplicate':
        work['held'] = [{'number': 2, 'state': 'waiting-human'}]
    config = Config(language=language, view_glyphs='ascii', lanes={'work': {}})
    body = '\n'.join(''.join(value for value, _ in line) for line in lane_bubble.render(
        snapshot, config, tmp_path, width=120, now=NOW, state={'explanation_target': ('acorn', 2)}))
    expected = (('Parked in lane work.', 'work 레인에서 보류 중이에요.') if evidence == 'parked'
                else ('I will ask in the planning session.', '기획 세션에서 물어볼게요.'))
    assert expected[language == 'ko'] in body, body
    assert 'test slot' not in body and '시험 자리' not in body
    block = '\n'.join(''.join(value for value, _ in line) for line in lane_blocks.render(
        snapshot, config, tmp_path, width=120, now=NOW))
    assert block.count('Canonical waiting context') == 1, block
    assert 'waiting its turn' not in block and '순서를 기다려요' not in block


@pytest.mark.parametrize('language', ['en', 'ko'])
def test_selected_authored_wait_uses_the_ascii_mark_when_its_reason_is_clipped(language, tmp_path, monkeypatch):
    from test_watch_authored_waits import scenario, WAIT, VALUES, NOW as captured
    from foragent import status

    config, store, tracker = scenario(tmp_path, monkeypatch, language)
    tracker.update_issue(2, title='Choice', body=WAIT.replace(VALUES[0], 'Long reason ' * 40))
    store.refresh_local_issues(tracker.list_issues('all'), [], config.lanes, captured)
    snapshot = watch.collect(status.collect(store, config, captured), None, store, config, captured)
    assert snapshot.waiting_facts[2]['authored']['reason'].startswith('Long reason')
    frame = watch.render_frame(snapshot, config, tmp_path, width=50, now=captured,
                               state={'explanation_target': ('acorn', 2)})
    lines = frame.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith('+'))
    body = '\n'.join(lines[top:top + 8])
    assert '...' in body and '…' not in body
    assert 'Long reason' in body and all(watch._cells(line) <= 50 for line in lines)


@pytest.mark.parametrize('mark', ['…', '...'])
@pytest.mark.parametrize('width', [0, 1, 2, 3, 8])
def test_shared_elision_bounds_cells_and_marks_forced_omissions(mark, width):
    from foragent.watch_scene import fit_marked

    cut = fit_marked([('한글 suffix', 'bold')], width, mark)
    value = ''.join(part for part, _ in cut)
    assert watch._cells(value) <= width
    if width >= watch._cells(mark):
        assert value.endswith(mark)
    else:
        assert value == mark[:width]
    literal = [('literal  data', 'dim')]
    assert fit_marked(literal, 30, mark) == literal
    forced = lane_bubble._clip([[('x', 'bold')], [('hidden', 'dim')]], 8, mark, limit=1)
    assert ''.join(part for part, _ in forced[0]) == 'x' + mark


def test_fitting_explanation_rows_remove_controls_but_keep_authored_spaces():
    rows = lane_bubble._explanation_rows([
        [('lane a\nb\tc\x1b[31m', '')], [('Waiting: Choice  pending', 'dim')]], 80, '...')
    assert [''.join(value for value, _ in line) for line in rows] == [
        'lane a b c[31m', 'Waiting: Choice  pending']


@pytest.mark.parametrize('language', ['en', 'ko'])
@pytest.mark.parametrize('fact_lane', ['missing', None, '', 'removed', 'configured-only'])
@pytest.mark.parametrize('source', ['running', 'active-run', 'held-blocked', 'held-question', 'queued', 'representative'])
def test_unresolved_authored_lane_keeps_the_recorded_acorn_and_its_selection(
    language, fact_lane, source, tmp_path,
):
    from copy import deepcopy

    work = row()
    work.update(state='idle', issue=None, queued=[])
    item = {'number': 2, 'title': 'Choice'}
    if source == 'running':
        work.update(state='working', issue=2, phase='implementing', running=[item])
    elif source.startswith('held-'):
        state = 'blocked' if source == 'held-blocked' else 'waiting-human'
        work.update(state=state, issue=2, held=[{**item, 'state': state}])
    elif source == 'queued':
        work.update(state='queued', issue=2, queued=[item])
    elif source == 'representative':
        work.update(state='working', issue=2, title='Choice', phase='implementing')
    fact = {'title': 'Choice', 'authored': {
        'text': 'Original wait', 'reason': 'Locale pending',
        'next_condition': 'Record locale', 'responsible_role': 'planner'}}
    if fact_lane != 'missing':
        fact['lane'] = fact_lane
    snapshot = _snapshot(work, waiting_facts={2: fact})
    snapshot.status.update(summary={'working': 1, 'blocked': 0, 'waiting_human': 0, 'queued': 0}, history=[])
    snapshot.status['lanes']['rest'] = {**row('rest'), 'state': 'idle', 'issue': None, 'queued': []}
    if source == 'active-run':
        snapshot.status['active_runs'] = [{
            'number': 2, 'lane': 'work', 'phase': 'implementing', 'started_at': NOW.isoformat()}]
        snapshot.titles[2] = 'Choice'
    before = deepcopy(snapshot.waiting_facts)
    config = Config(language=language, view_glyphs='ascii', lanes={'work': {}, 'rest': {}, 'configured-only': {}})
    view, layout = {}, {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state=view, layout=layout)
    hits = [hit for hit in layout['hits'] if hit[-1] == ('acorn', 2)]
    assert len(hits) == 1, frame
    for value in ('Locale pending', 'Record locale', 'planner'):
        assert frame.count(value) == 1, frame
    if source == 'running':
        assert ('레인 1곳에서 이슈 1개를 작업하고 있어요.' if language == 'ko'
                else 'Working on 1 issue in 1 lane.') in frame, frame
    x, y, *_ = hits[0]
    watch.handle_mouse('left', x, y, layout, StateStore(tmp_path / 'state'), config, state=view)
    assert view['explanation_target'] == ('acorn', 2)
    selected = watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state=view)
    lines = selected.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith('+'))
    body = '\n'.join(lines[top:top + 8])
    assert '#2 Choice' in body and ('work 레인' if language == 'ko' else 'lane work') in body, body
    expected = {
        'held-blocked': ('handled by the orchestrator', '오케스트레이터가 보는 중'),
        'held-question': ('I will ask in the planning session.', '기획 세션에서 물어볼게요.'),
        'queued': ('Waiting: Locale pending', '기다리는 이유: Locale pending'),
    }.get(source, ('writing code', '코드를 쓰고 있어요'))
    assert expected[language == 'ko'] in body, body
    assert snapshot.waiting_facts == before


@pytest.mark.parametrize('language', ['en', 'ko'])
def test_resolved_authored_lane_still_routes_one_acorn_to_its_recorded_lane(language, tmp_path):
    work = row()
    work.update(state='working', issue=2, phase='implementing', running=work['queued'], queued=[])
    snapshot = _snapshot(work, waiting_facts={2: {
        'lane': 'work', 'authored': {'text': 'Canonical waiting context'}},
        7: {'lane': 'rest', 'title': 'Unapproved choice', 'authored': {'text': 'Another recorded wait'}}})
    snapshot.status.update(summary={'working': 1, 'blocked': 0, 'waiting_human': 0, 'queued': 1}, history=[])
    # A subsequently edited label must not add a duplicate in the other lane.
    snapshot.status['lanes']['rest'] = {**row('rest'), 'issue': 2, 'title': 'Edited label'}
    config = Config(language=language, view_glyphs='ascii', lanes={'work': {}, 'rest': {}})
    layout = {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, layout=layout)
    targets = [hit[-1] for hit in layout['hits'] if hit[-1][0] == 'acorn']
    assert targets.count(('acorn', 2)) == targets.count(('acorn', 7)) == 1, frame
    assert frame.count('Canonical waiting context') == frame.count('Another recorded wait') == 1, frame
    for number, lane in ((2, 'work'), (7, 'rest')):
        body = '\n'.join(''.join(value for value, _ in line) for line in lane_bubble.render(
            snapshot, config, tmp_path, width=120, now=NOW, state={'explanation_target': ('acorn', number)}))
        assert (f'{lane} 레인' if language == 'ko' else f'lane {lane}') in body, body


def _phases_the_runner_sets():
    """Every literal phase passed to the runner's phase setters, read from the source."""
    import ast
    from pathlib import Path

    import foragent

    setters = {"_phase", "in_phase", "_credited_wait", "WaitPhase"}
    found = set()
    for path in Path(foragent.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in setters:
                found |= {leaf.value for arg in node.args for leaf in ast.walk(arg)
                          if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)}
    return found


def _worker(phase, state="working"):
    return {"number": 7, "class": "wait" if phase in WAIT_PHASES or state == "blocked" else "working",
            "phase": phase, "state": state}


WAIT_PHASES = frozenset({"overlap-wait", "test-slot-wait", "baseline-wait", "limit-wait", "executor-wait"})


def test_the_source_scan_finds_the_phases_of_each_setter():
    """A scan that misses a setter would leave a new phase untested."""
    assert {"implementing", "resolving-conflict", "checking", "reviewing", "ci", "merging", "baseline",
            "baseline-wait", "overlap-wait", "test-slot-wait", "executor-wait",
            "merge-lock-wait"} <= _phases_the_runner_sets()


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("phase", sorted(_phases_the_runner_sets() | WAIT_PHASES))
def test_every_phase_the_runner_can_set_has_a_word_on_the_acorn(phase, language):
    from foragent import watch_scene

    assert watch_scene.WAIT_PHASES == WAIT_PHASES
    snapshot = _snapshot(row())
    current, _ = watch_scene.lane_items(snapshot, {
        "slug": "work", "state": "working", "issue": 7, "phase": phase, "running": [{"number": 7}]})
    word = lane_blocks._word(current[0], language)
    assert word and word.strip(), f"{phase} leaves the acorn with only its number"
    assert word != watch.text("lane_blocks_word_wait", language), f"{phase} has no word of its own"
    assert f"#7 {word}" in "".join(value for value, _ in lane_blocks._acorn(current[0], language, 0, True))


@pytest.mark.parametrize("language, phase, word", [
    ("en", "overlap-wait", "wait: shared files"), ("ko", "overlap-wait", "대기: 공유 파일"),
    ("en", "test-slot-wait", "wait: test slot"), ("ko", "test-slot-wait", "대기: 시험 자리"),
    ("en", "baseline-wait", "wait: baseline"), ("ko", "baseline-wait", "대기: 기준선"),
    ("en", "limit-wait", "wait: usage limit"), ("ko", "limit-wait", "대기: 사용량 한도"),
    ("en", "executor-wait", "wait: model retry"), ("ko", "executor-wait", "대기: 모델 재시도"),
    ("en", None, "prep"), ("ko", None, "준비"),
])
def test_a_worker_says_what_it_waits_for_or_that_it_is_preparing(language, phase, word):
    assert lane_blocks._word(_worker(phase), language) == word


@pytest.mark.parametrize("language", ["en", "ko"])
def test_an_unknown_phase_or_a_block_shows_the_generic_wait_word_never_blank(language):
    generic = watch.text("lane_blocks_word_wait", language)
    assert generic and lane_blocks._word(_worker("a-phase-from-tomorrow"), language) == generic
    assert lane_blocks._word(_worker("implementing", "blocked"), language) == generic


def test_queued_work_still_says_nothing_beside_its_number():
    for kind in ("next", "authored"):
        assert lane_blocks._word({"class": kind, "phase": None}, "en") == ""


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("phase", sorted(WAIT_PHASES))
def test_a_wait_phase_explains_itself_on_click(phase, language, tmp_path):
    work = {"slug": "work", "state": "working", "issue": 7, "phase": phase, "running": [{"number": 7}]}
    rows = lane_bubble.explain(("acorn", 7), _snapshot(work), Config(language=language), tmp_path,
                               now=NOW, width=80, state={})
    value = "\n".join("".join(part for part, _ in line) for line in rows)
    assert watch.text(phase, language) in value

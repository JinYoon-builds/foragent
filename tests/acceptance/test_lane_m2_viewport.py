"""Viewport composition without depending on future lane scene modules."""
from types import SimpleNamespace
from test_skills_autosync import isolated_home
from test_squirrel_running import single_worker, view_offline


def test_viewport_pins_explicit_header_and_usage_then_hints_and_remaps_hits(tmp_path, monkeypatch):
    from foragent import watch_layout, watch
    snapshot, config = single_worker()
    def section(name, order, lines, pin=None):
        def render(*args, **context):
            if name == 'body':
                context['hits'].extend((0, y, 3, y+1, ('acorn', y+1)) for y in range(20))
                context['marks'].extend((y, y+1, 'work') for y in range(20))
            return [[(line, '')] for line in lines]
        return SimpleNamespace(__name__='foragent.watch_sections.'+name, ORDER=order,
                 render=render, PIN=pin, GAP=False, GAP_AFTER=False)
    monkeypatch.setattr(watch_layout, '_sections', lambda: [
        section('head', 1, ['HEADER'], 'header'),
        section('body', 2, [f'BODY {i}' for i in range(20)]),
        section('foot', 3, ['USAGE', 'HINTS'], 'footer')])
    state, layout = {}, {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=80, height=9, state=state, layout=layout)
    lines = frame.splitlines()
    assert len(lines) == 9 and lines[0] == 'HEADER' and lines[-2:] == ['USAGE', 'HINTS']
    assert watch.hit_test(layout, 0, 1) == ('acorn', 1)
    watch.scroll(state, 100)
    frame = watch.render_frame(snapshot, config, tmp_path, width=80, height=9, state=state, layout=layout)
    lines = frame.splitlines()
    assert lines[0] == 'HEADER' and lines[-2:] == ['USAGE', 'HINTS']
    assert 'BODY 19' in lines
    assert any(watch.hit_test(layout, 0, y) == ('acorn', 20) for y in range(1, 7))
    assert not any(watch.hit_test(layout, 0, y) for y in (0, 6, 7, 8))


def test_position_line_retains_the_localized_selection_hint():
    from foragent.watch_viewport import position_line
    from foragent.i18n import text
    for language in ('ko','en'):
        line = ''.join(value for value, _ in position_line(2,7,20,240,language)).rstrip()
        assert line == text('acorns_position',language,first=2,last=7,total=20) + ' · ' + text('acorns_select',language)

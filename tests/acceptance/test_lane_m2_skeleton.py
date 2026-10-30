"""Acceptance for the shared lane seams; minimal behavior only."""
from dataclasses import replace
from datetime import datetime, UTC
import inspect
import pytest
from pathlib import Path
from types import SimpleNamespace

from test_skills_autosync import isolated_home
from test_squirrel_running import single_worker, view_offline


def test_snapshot_exposes_defaulted_fact_maps_and_explicit_checkout_root():
    from foragent import watch_data
    snapshot, _ = single_worker()
    assert snapshot.lane_facts == snapshot.role_facts == snapshot.waiting_facts == snapshot.park_facts == {}
    assert inspect.signature(watch_data.collect).parameters['repo_root'].default is None
    clone = replace(snapshot)
    clone.waiting_facts[7] = {'kind': None, 'blocking_issues': [], 'overlapping_files': []}
    other, _ = single_worker()
    assert other.waiting_facts == {}


def test_checkout_root_is_scoped_nested_and_restored(tmp_path):
    from foragent.watch_data import COLLECTION_ROOT, collection_root
    assert COLLECTION_ROOT.get() is None
    with collection_root(tmp_path) as root:
        assert root == tmp_path and COLLECTION_ROOT.get() == tmp_path
        with collection_root(tmp_path / 'nested'):
            assert COLLECTION_ROOT.get() == tmp_path / 'nested'
        assert COLLECTION_ROOT.get() == tmp_path
    assert COLLECTION_ROOT.get() is None


def test_new_art_exports_preserve_old_pose_keys_and_use_colored_spans():
    from foragent import watch_art as art
    assert set(art.LANE_POSES) == {'gnaw', 'sit', 'wave', 'sleep'}
    assert art.PALETTE['D'] == 250
    for grid, width in ((art.lookout(0), 11), (art.lying_asleep(0), 9)):
        assert len(grid) == 8 and all(len(row) == width for row in grid)
    row = art.sideways_acorn(7, 'write', 'working', tick=0)
    assert ''.join(value for value, _ in row) == '◖█#7 write▌'
    assert ''.join(value for value, _ in art.sideways_acorn(7, 'write', 'working', ascii=True)) == '[#7 write]'


def test_pixel_transport_normalizes_lower_halves_without_ansi():
    from foragent.watch_art import pixel_rows
    assert pixel_rows([[None, 'a', 'a', None], ['b', None, 'b', None]], {'a': 130, 'b': 240}) == [[
        ('▄', ('art', 240, None)), ('▀', ('art', 130, None)),
        ('▀', ('art', 130, 240)), (' ', ('art', None, None)),
    ]]


def test_painter_decodes_raw_colors_and_preserves_named_attributes():
    from foragent.watch_terminal import style_attribute
    calls = []
    def pair(fg, bg):
        calls.append((fg, bg))
        return 123
    assert style_attribute(('art', 220, None), styles={}, pair=pair) == 123
    assert calls == [(220, None)]
    assert style_attribute('working', styles={'working': 55}, pair=pair) == 55
    assert style_attribute('unknown', styles={}, pair=pair) == 0


def test_section_provider_dispatch_is_optional_ordered_and_preserves_caller_state(tmp_path, monkeypatch):
    from foragent import watch_explanations, watch_layout
    snapshot, config = single_worker()
    seen, state = [], {'explanation_target': ('role', 'docs')}
    def provider(target, actual, cfg, root, *, now, width, state):
        seen.append((target, actual, cfg, root, now, width, state))
        return [[('caller words', 'working')]]
    monkeypatch.setattr(watch_layout, '_sections', lambda: [
        SimpleNamespace(ORDER=1), SimpleNamespace(ORDER=2, explain=provider)])
    assert watch_explanations.explain(('role', 'docs'), snapshot, config, tmp_path,
           now=snapshot.captured_at, width=100, state=state) == [[('caller words', 'working')]]
    assert seen == [(('role', 'docs'), snapshot, config, tmp_path, snapshot.captured_at, 100, state)]
    assert state == {'explanation_target': ('role', 'docs')}
    monkeypatch.setattr(watch_layout, '_sections', lambda: [SimpleNamespace(ORDER=1)])
    assert watch_explanations.explain(('role', 'docs'), snapshot, config, tmp_path,
           now=snapshot.captured_at, width=100, state=state) is None


def test_empty_replacement_discards_hits_marks_and_still_invokes_legacy(tmp_path, monkeypatch):
    from foragent import watch_layout
    snapshot, config = single_worker()
    calls = []
    def legacy_render(*args, **context):
        calls.append('legacy')
        context['hits'].append((0, 0, 2, 1, ('acorn', 991)))
        context['marks'].append((0, 991, 'work'))
        return [[('legacy', '')]]
    def replacement(*args, **context):
        calls.append('replacement')
        return []
    legacy = SimpleNamespace(__name__='foragent.watch_sections.test_legacy', ORDER=1,
                             render=legacy_render, KEEP=True, SIDE=1, TIGHT=True)
    new = SimpleNamespace(__name__='foragent.watch_sections.test_new', ORDER=2,
                          render=replacement, REPLACES=(legacy.__name__, 'foragent.watch_sections.absent'))
    monkeypatch.setattr(watch_layout, '_sections', lambda: [legacy, new])
    state, layout = {}, {}
    assert watch_layout._frame(snapshot, config, tmp_path, tick=0, width=80,
               selected=None, now=snapshot.captured_at, state=state, layout=layout) == []
    assert calls == ['legacy', 'replacement']
    assert layout['hits'] == [] and state['rows'] == []


def test_explanation_selection_closes_without_quitting_or_opening_old_box(tmp_path):
    from foragent.watch import handle_mouse, handle_key
    from foragent.state import StateStore
    snapshot, config = single_worker()
    state = {}
    store = StateStore(tmp_path / 'state')
    layout = {'hits': [(0, 0, 3, 1, ('role', 'docs'))]}
    handle_mouse('left', 1, 0, layout, store, config, state=state)
    assert state['explanation_target'] == ('role', 'docs')
    assert state.get('issue') is None
    assert handle_key('esc', store, config, state=state) is False
    assert state.get('explanation_target') is None
    assert handle_key('esc', store, config, state=state) is True


def test_usage_parts_can_omit_capacity_without_duplicating_formatter_logic():
    from foragent.watch_sections import usage
    snapshot, config = single_worker()
    snapshot.meter = {'memory': 'critical', 'running': 3, 'budget': 7}
    full, left = usage._parts(snapshot, config, {}, include_capacity=True)
    short, short_left = usage._parts(snapshot, config, {}, include_capacity=False)
    assert short_left == left and len(full) == len(short) + 1
    assert short[:2] == full[:2] and short[-1] == full[-1]


@pytest.mark.parametrize('colors', [8,256])
def test_named_styles_register_separately_without_growing_the_base_palette(tmp_path,monkeypatch,colors):
    import curses
    from io import StringIO
    from unittest.mock import Mock
    from foragent import watch, watch_terminal
    from foragent.state import StateStore
    snapshot, config = single_worker()
    screen, initialized, painted = Mock(), [], {}
    screen.addstr.side_effect = lambda y,x,value,attr: painted.__setitem__(value,attr)
    screen.getmaxyx.return_value = (10,80)
    screen.getch.return_value = ord('q')
    monkeypatch.setattr(watch.sys,'stdout',StringIO())
    monkeypatch.setattr(watch,'time',SimpleNamespace(monotonic=lambda:0))
    monkeypatch.setattr(watch,'_installed_build',lambda:'unknown')
    monkeypatch.setattr(watch,'_frame',lambda *a,**k: [[(name,name)] for name in ('working','question','next','separator')])
    monkeypatch.setattr(curses,'COLORS',colors,raising=False)
    monkeypatch.setattr(curses,'COLOR_PAIRS',64,raising=False)
    monkeypatch.setattr(curses,'has_colors',lambda:True)
    monkeypatch.setattr(curses,'start_color',lambda:None)
    monkeypatch.setattr(curses,'use_default_colors',lambda:None)
    monkeypatch.setattr(curses,'color_pair',lambda i:i<<8)
    monkeypatch.setattr(curses,'init_pair',lambda i,fg,bg:initialized.append((i,fg,bg)))
    monkeypatch.setattr(curses,'curs_set',lambda *a:None)
    monkeypatch.setattr(curses,'mousemask',lambda *a:None)
    monkeypatch.setattr(curses,'mouseinterval',lambda *a:None)
    base = watch.color_pairs(colors)
    assert len(base) == 7 and all(bg == -1 for fg,bg in base)
    watch_terminal._terminal(screen,lambda:snapshot,snapshot,config,tmp_path,
        lambda:snapshot.captured_at,StateStore(tmp_path/'state'))
    assert [(fg,bg) for _,fg,bg in initialized[:7]] == base
    registered = dict((i,(fg,bg)) for i,fg,bg in initialized)
    assert {8:(203 if colors==256 else curses.COLOR_RED,-1),
            9:(240 if colors==256 else curses.COLOR_WHITE,-1),
            10:(242 if colors==256 else curses.COLOR_WHITE,-1)}.items() <= registered.items()
    working_pair = (painted['working'] & curses.A_COLOR) >> 8
    assert registered[working_pair] == (220 if colors==256 else curses.COLOR_YELLOW,-1)
    assert watch.color_pairs(colors) == base




def test_pin_metadata_uses_surviving_absolute_rows(tmp_path, monkeypatch):
    from foragent import watch_layout
    snapshot, config = single_worker()
    def section(name, order, rows, pin=None, replaces=()):
        return SimpleNamespace(__name__='foragent.watch_sections.'+name, ORDER=order,
            PIN=pin, REPLACES=replaces, GAP=False, GAP_AFTER=False,
            render=lambda *a, **k: [[(row, '')] for row in rows])
    monkeypatch.setattr(watch_layout, '_sections', lambda: [
        section('old', 1, ['SUPPRESSED'], 'header'),
        section('head', 2, ['HEAD'], 'header', ('foragent.watch_sections.old',)),
        section('body', 3, ['A', 'B']), section('foot', 4, ['USAGE', 'HINTS'], 'footer')])
    state = {}
    frame = watch_layout._frame(snapshot, config, tmp_path, tick=0, width=80,
        selected=None, now=snapshot.captured_at, state=state)
    assert state['pinned_rows'] == {'header': [0], 'footer': [3, 4]}
    assert [''.join(v for v, _ in line).strip() for line in frame] == ['HEAD','A','B','USAGE','HINTS']


@pytest.mark.parametrize('colors', [8, 256])
def test_live_structured_acorn_links_keep_foreground_background_and_bold(tmp_path, monkeypatch, colors):
    import curses
    from io import StringIO
    from unittest.mock import Mock
    from foragent import watch, watch_terminal
    from foragent.state import StateStore
    from test_presence_session_start import _colored_cells, _rgb
    snapshot, config = single_worker()
    config = replace(config, repo='owner/repo')
    output, initialized, painted = StringIO(), [], []
    screen = Mock()
    screen.getmaxyx.return_value = (10, 80)
    screen.getch.return_value = ord('q')
    screen.addstr.side_effect = lambda y, x, value, attr: painted.append((value, attr))
    monkeypatch.setattr(watch.sys, 'stdout', output)
    monkeypatch.setattr(watch, 'time', SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, '_installed_build', lambda: 'unknown')
    monkeypatch.setattr(watch, '_frame', lambda *a, **k: [[('#7 write', ('art',232,220))]])
    monkeypatch.setattr(curses, 'COLORS', colors, raising=False)
    monkeypatch.setattr(curses, 'COLOR_PAIRS', 64, raising=False)
    monkeypatch.setattr(curses, 'has_colors', lambda: True)
    monkeypatch.setattr(curses, 'start_color', lambda: None)
    monkeypatch.setattr(curses, 'use_default_colors', lambda: None)
    monkeypatch.setattr(curses, 'color_pair', lambda i: i << 8)
    monkeypatch.setattr(curses, 'init_pair', lambda i, fg, bg: initialized.append((i,fg,bg)))
    monkeypatch.setattr(curses, 'curs_set', lambda *a: None)
    monkeypatch.setattr(curses, 'mousemask', lambda *a: None)
    monkeypatch.setattr(curses, 'mouseinterval', lambda *a: None)
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path,
        lambda: snapshot.captured_at, StateStore(tmp_path/'state'))
    fg, bg = (232,220) if colors == 256 else (0,3)
    assert (11,fg,bg) in initialized and len(initialized) == 8
    assert any(value == '#7 write' and attr & curses.A_BOLD for value, attr in painted)
    linked = output.getvalue()
    start = linked.index('\x1b]8;;https://github.com/owner/repo/issues/7')
    before, after = linked[:start], linked[start:]
    attrs = before[before.rfind('H')+1:]
    assert '\x1b[0m' in attrs and any('1' in seq.split('m')[0].split(';') for seq in attrs.split('\x1b[')[1:])
    assert '\x1b[0m' in after
    # The existing terminal parser checks actual ink, not just presence of a colour escape.
    import re
    printable = re.sub(r'\x1b\]8;;[^\x1b]*\x1b\\', '', attrs + after.split('\x1b[0m')[0])
    cells = _colored_cells(printable)
    assert [cell for cell in cells if cell[0] == '#'] == [('#',_rgb(fg),_rgb(bg))]

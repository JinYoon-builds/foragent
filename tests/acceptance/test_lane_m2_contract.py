"""Acceptance test for lane-m2-contract. Locked by Foragent; do not edit."""

from test_acorn_interact import hit_positions
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_sections import extra_sections, write_section, section_modules


def draw(snapshot, config, root, *, width=160, state=None, layout=None, **kwargs):
    from foragent.watch import render_frame
    return plain_frame(render_frame(snapshot, config, root, width=width,
                                    state=state, layout=layout, **kwargs))


def bubble(frame):
    lines = plain_frame(frame).splitlines()
    first = next((i for i, line in enumerate(lines) if '╭' in line and '╮' in line), None)
    assert first is not None, 'the redesigned frame needs the orchestrator bubble'
    assert first + 7 < len(lines) and '╰' in lines[first + 7]
    body = []
    for line in lines[first+1:first+7]:
        left = min(i for i, ch in enumerate(line) if ch in '│┤')
        body.append(line[left+1:line.rindex('│')].strip())
    return '\n'.join(body).strip()


def pick(snapshot, config, root, store, target, *, width=160, state=None):
    from foragent.watch import handle_mouse
    state = {'store': store} if state is None else state
    layout = {}
    frame = draw(snapshot, config, root, width=width, state=state, layout=layout)
    hits = hit_positions(layout, frame, width)
    assert target in hits, f'public target {target!r} is missing from the rendered frame'
    handle_mouse('left', *hits[target], layout, store, config, state=state)
    return draw(snapshot, config, root, width=width, state=state), state


def acorn_row(frame, number):
    import re
    rows = [line for line in plain_frame(frame).splitlines()
            if re.search(rf'(?<!\d)#{number}(?!\d)', line)
            and ('▌' in line or '[' in line)]
    assert len(rows) == 1, f'#{number} needs one sideways-acorn row (or its explicit ASCII fallback)'
    return rows[0]


def test_discovered_section_can_replace_a_legacy_contribution_without_removing_discovery(tmp_path, monkeypatch):
    import importlib
    from foragent import watch_layout
    from test_squirrel_running import single_worker

    snapshot, config = single_worker()
    with extra_sections(tmp_path, monkeypatch) as (package, directory):
        legacy = importlib.import_module('foragent.watch_sections.lanes')
        monkeypatch.setattr(legacy, 'render', lambda *a, **k: [[('LEGACY_ROWS_PROBE', '')]])
        order = max(module.ORDER for module in section_modules(package)) + 10
        write_section(directory, 'acceptance_m2_replacement', order, labels={'en':'NEW_ROWS_PROBE','ko':'NEW_ROWS_PROBE'})
        path = directory/'acceptance_m2_replacement.py'
        path.write_text(path.read_text() + "\nREPLACES = ('foragent.watch_sections.lanes',)\n")
        importlib.invalidate_caches()
        frame = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path))
        assert 'NEW_ROWS_PROBE' in frame
        assert 'LEGACY_ROWS_PROBE' not in frame, 'replacement is a section composition seam, not a second screen engine'

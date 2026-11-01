import re
from types import SimpleNamespace

import pytest
from runner_support import run_case  # noqa: F401

from foragent.issue import parse_body
from foragent.run_checks import CheckResult
from foragent.run_pr_body import _pr_body


def test_check_commands_are_escaped_and_failed_checks_are_visible():
    body = _pr_body(7, parse_body(''), [CheckResult('a | b\n<x> & `y`', 1, False, '')], 2, [],
                    outside_lane=['other.py'], skills_line='Skills used: none', try_text='Try this')
    assert '| <code>a &#124; b<br>&lt;x> &amp; &#96;y&#96;</code> | fail |' in body
    assert 'Closes #7' in body and '- `other.py`' in body
    assert body.endswith('## Try it\n\nTry this')


def test_searched_list_is_a_section_only_when_the_lane_wrote_one():
    body = _pr_body(7, parse_body(''), [], 1, [], searched='- looked for stale writers in src/: found 2')
    assert '\n## Searched\n\n- looked for stale writers in src/: found 2\n' in body
    assert '## Searched' not in _pr_body(7, parse_body(''), [], 1, [])


def test_render_reads_the_latest_searched_file_from_the_worktree(tmp_path):
    from types import SimpleNamespace

    from foragent.run_publish_pr import _searched

    attempt = SimpleNamespace(worktree=tmp_path)
    assert _searched(attempt) == ''
    (tmp_path / '.foragent').mkdir()
    (tmp_path / '.foragent/SEARCHED.md').write_text('- grep writers: 3 found\n', encoding='utf-8')
    assert _searched(attempt) == '- grep writers: 3 found'


def test_fitter_keeps_fitting_bodies_verbatim_including_multibyte_boundary():
    from foragent.pr_body_limit import fit_pr_body

    for body in ('small\n', '증' * 65536):
        assert fit_pr_body(body) == body


def test_fitter_preserves_required_content_and_complete_marker_pairs():
    from foragent.pr_body_limit import fit_pr_body

    required = [
        'Closes #7\n', '| check | pass |\n', '## Review\n',
        'Round 1: VERDICT: PASS\n', '- [ ] Human approval\n',
        'Opened by Foragent gate (attempt 1)\n',
        '<!-- foragent:v1 -->\n',
        '## What changed\n\nThe output is now correct.\n\n',
        '<!-- foragent:risk-notes -->\nrisk details\n<!-- /foragent:risk-notes -->\n',
        '<!-- foragent:lane-decisions -->\nDECIDE: keep\n<!-- /foragent:lane-decisions -->\n',
    ]
    body = required[0] + '## Rebuttals\n\n' + 'evidence\n' * 20000 + ''.join(required[1:])
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    assert len(fitted) <= 65536
    for part in required:
        assert part in fitted
    assert '_full text: pr-bodies/original.md_' in fitted
    assert fit_pr_body(fitted) == fitted
    assert fitted != body


def test_fitter_shortens_batch_children_without_changing_parent_semantics():
    from foragent.pr_body_limit import fit_pr_body

    body = 'Sub-issues of #99\n\n## Children\n\n' + '- child title\n' * 10000
    fitted = fit_pr_body(body)
    assert len(fitted) <= 65536
    assert fitted.startswith('Sub-issues of #99\n\n## Children\n')
    assert 'Closes' not in fitted
    assert 'full text' in fitted


def test_tracker_retains_each_original_and_fits_every_write(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, RecordingTracker

    store = StateStore(tmp_path)
    remote = FakeTracker()
    calls = []
    for operation in ('create_pr', 'update_pr'):
        send = getattr(remote, operation)

        def checked(*args, _send=send, _operation=operation, **kwargs):
            assert len(kwargs['body']) <= 65536
            calls.append(_operation)
            return _send(*args, **kwargs)

        monkeypatch.setattr(remote, operation, checked)
    writer = RecordingTracker(remote, SimpleNamespace(store=store, config=Config()))
    original = 'Closes #7\n\n## Summary\n\nShort summary.\n\n' + '증거\n' * 30000
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=original)
    first = remote.created_prs[0].body
    writer.update_pr(pr, base='main', title='Feature', body=original + '\nUpdate')
    second = remote.created_prs[0].body
    assert calls == ['create_pr', 'update_pr']
    assert len(first) <= 65536 and len(second) <= 65536
    for fitted, expected in ((first, original), (second, original + '\nUpdate')):
        retained = [p for p in (tmp_path / 'pr-bodies').iterdir()
                    if p.relative_to(tmp_path).as_posix() in fitted]
        assert len(retained) == 1
        assert retained[0].read_text(encoding='utf-8') == expected


def _fit_keeps_locked_parts(body):
    from foragent.pr_body_limit import fit_pr_body

    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    assert len(fitted) <= 65536
    lines = fitted.splitlines()
    assert 'Closes #7' in lines
    for name in ('risk-notes', 'lane-decisions'):
        assert lines.count(f'<!-- foragent:{name} -->') == lines.count(f'<!-- /foragent:{name} -->') == 1
        assert lines.index(f'<!-- foragent:{name} -->') < lines.index(f'<!-- /foragent:{name} -->')
    assert '_full text: pr-bodies/original.md_' in lines
    return fitted


PAIRS = ('<!-- foragent:risk-notes -->\nkeep risk\n<!-- /foragent:risk-notes -->\n'
         '<!-- foragent:lane-decisions -->\nkeep decision\n<!-- /foragent:lane-decisions -->\n')


@pytest.mark.parametrize('line', [
    '| finding | detail that goes on |\n', '- [ ] check this item\n', '## A heading line\n',
    'Round 1: VERDICT: PASS\n', 'Opened by Foragent gate (attempt 1)\n',
    'Sub-issues of #99 and so on\n', 'Closes #7\n', '<!-- foragent:write id=abc -->\n',
])
@pytest.mark.parametrize('closing_last', [False, True])
def test_required_looking_lines_alone_over_the_limit_keep_locked_parts(line, closing_last):
    # The closing reference and the pairs sit after the flood, where a tail cut would hit them.
    flood = line * (70000 // len(line) + 1)
    assert len(flood) > 65536
    body = (flood + PAIRS + 'Closes #7\n' if closing_last else 'Closes #7\n' + flood + PAIRS)
    fitted = _fit_keeps_locked_parts(body)
    assert 'keep risk' in fitted and 'keep decision' in fitted


def test_long_table_shortens_by_whole_rows_and_spares_small_prose():
    table = '| Finding | Where |\n| --- | --- |\n' + ''.join(f'| finding {i} | file {i} |\n' for i in range(5000))
    body = 'Closes #7\n\n## Review\n\n' + table + '\nA short reviewer note.\n' + PAIRS
    fitted = _fit_keeps_locked_parts(body)
    assert '| Finding | Where |\n| --- | --- |\n| finding 0 | file 0 |\n' in fitted
    assert 'A short reviewer note.' in fitted
    assert '| finding 4999 | file 4999 |' not in fitted
    for line in fitted.splitlines():
        if line.startswith('| finding'):
            assert re.fullmatch(r'\| finding \d+ \| file \d+ \|', line)


def test_expandable_text_goes_before_required_lines_and_marker_text():
    body = ('Closes #7\n\n| Done when command | Result |\n| --- | --- |\n'
            + ''.join(f'| <code>check {i}</code> | pass |\n' for i in range(20))
            + 'VERDICT: PASS\n' + '- [ ] Human approval\n' + PAIRS + 'evidence\n' * 20000)
    fitted = _fit_keeps_locked_parts(body)
    for kept in ('| <code>check 19</code> | pass |', 'VERDICT: PASS', '- [ ] Human approval',
                 'keep risk', 'keep decision'):
        assert kept in fitted


def test_oversized_marker_text_shortens_inside_its_complete_pair():
    body = ('Closes #7\n<!-- foragent:risk-notes -->\n' + 'risk\n' * 20000
            + '<!-- /foragent:risk-notes -->\n<!-- foragent:lane-decisions -->\nkeep decision\n'
            '<!-- /foragent:lane-decisions -->\n')
    fitted = _fit_keeps_locked_parts(body)
    assert 'keep decision' in fitted and 'risk\nrisk\n' in fitted


def test_markers_past_the_limit_never_leave_half_a_pair_or_lose_the_first_closing():
    from foragent.pr_body_limit import fit_pr_body

    body = 'Closes #7\n' + ''.join(
        f'<!-- foragent:p{i} -->\nx\n<!-- /foragent:p{i} -->\n' for i in range(3000))
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    assert len(fitted) <= 65536
    assert fitted.startswith('Closes #7\n')
    assert fitted.endswith('_full text: pr-bodies/original.md_\n')
    assert re.findall(r'<!-- foragent:(p\d+) -->', fitted) == re.findall(r'<!-- /foragent:(p\d+) -->', fitted)


def test_quoted_opener_does_not_swallow_the_real_pair():
    body = ('Closes #7\nQuoted <!-- foragent:risk-notes --> in prose\n' + 'evidence\n' * 10000
            + '<!-- foragent:risk-notes -->\nkeep risk\n<!-- /foragent:risk-notes -->\n')
    from foragent.pr_body_limit import fit_pr_body

    assert 'keep risk' in fit_pr_body(body)


SUMMARY_CARD = '## What changed\nThe short summary.\n## Try it\nkind: cli\n'
FLOOD = {
    'risk': {'risk_notes': 'risk\n' * 20000},
    'decisions': {'lane_decisions': 'DECIDE: keep\n' * 6000},
    'headings': {'searched': '## quoted heading\n' * 5000},
    'checkboxes': {'searched': '- [ ] quoted box\n' * 5000},
    'verdicts': {'searched': 'quoted VERDICT: FAIL\n' * 5000},
    'gate-lines': {'searched': 'Opened by Foragent gate (quoted)\n' * 5000},
    'references': {'searched': 'Closes #7\n' * 8000},
    'lone-markers': {'searched': '<!-- foragent:write id=abc -->\n' * 3000},
    'table-rows': {'searched': '| a | b |\n' * 9000},
}


def _rendered(scope, **flood):
    from types import SimpleNamespace

    from foragent.run_review import ReviewResult

    parsed = SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Human approval')])
    return _pr_body(
        7, parsed, flood.pop('checks', [CheckResult('real check', 0, False, '')]), 1,
        [ReviewResult('PASS', '')], SUMMARY_CARD, review_scope=scope,
        **{'risk_notes': 'keep risk', 'lane_decisions': 'keep decision', **flood},
    )


def _assert_required_survive(fitted, *, checks=True):
    assert len(fitted) <= 65536
    lines = fitted.splitlines()
    assert 'Closes #7' in lines
    assert 'The short summary.' in lines
    assert 'VERDICT: PASS' in fitted
    assert re.search(r'^## Needs a human check\n\s*- \[ \] Human approval$', fitted, re.MULTILINE)
    assert 'Opened by Foragent gate (attempt 1)' in lines
    assert '_full text: pr-bodies/original.md_' in lines
    if checks:
        assert '| <code>real check</code> | pass |' in lines


@pytest.mark.parametrize('name', FLOOD)
def test_one_oversized_kind_leaves_every_other_required_part_whole(name):
    from foragent.pr_body_limit import fit_pr_body

    body = _rendered('pr-body', **FLOOD[name])
    assert len(body) > 65536
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    _assert_required_survive(fitted)
    if name != 'checkboxes':
        from foragent.inspect_data import _human_checks

        assert _human_checks(fitted) == '- [ ] Human approval'
    for marker in ('risk-notes', 'lane-decisions'):
        assert fitted.splitlines().count(f'<!-- foragent:{marker} -->') == 1
        assert fitted.splitlines().count(f'<!-- /foragent:{marker} -->') == 1
    if name != 'risk':
        assert 'keep risk' in fitted
    if name != 'decisions':
        assert 'keep decision' in fitted


def test_oversized_checks_table_and_checklist_leave_the_rest_whole():
    from types import SimpleNamespace

    from foragent.pr_body_limit import fit_pr_body

    checks = [CheckResult(f'check {i}', 0, False, '') for i in range(4000)]
    fitted = fit_pr_body(_rendered('pr-body', checks=checks), notice='_full text: pr-bodies/original.md_')
    _assert_required_survive(fitted, checks=False)
    assert '| <code>check 0</code> | pass |' in fitted

    items = [SimpleNamespace(text=f'box {i}') for i in range(8000)]
    body = _pr_body(7, SimpleNamespace(done_when_checklist=items), [CheckResult('real check', 0, False, '')],
                    1, [], SUMMARY_CARD, review_scope='pr-body', risk_notes='keep risk')
    assert len(body) > 65536
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    assert 'box 0' in fitted and 'The short summary.' in fitted and 'keep risk' in fitted
    assert 'Opened by Foragent gate (attempt 1)' in fitted


@pytest.mark.parametrize('inner_first', [True, False])
def test_nested_and_crossing_pairs_keep_both_delimiters(inner_first):
    from foragent.pr_body_limit import fit_pr_body

    inner = ('<!-- foragent:lane-decisions -->\n' + 'DECIDE: keep\n' * 6000
             + '<!-- /foragent:lane-decisions -->\n')
    if inner_first:
        body = f'Closes #7\n<!-- foragent:risk-notes -->\nrisk\n{inner}<!-- /foragent:risk-notes -->\n'
    else:  # The pairs cross instead of nesting.
        body = ('Closes #7\n<!-- foragent:risk-notes -->\n<!-- foragent:lane-decisions -->\n'
                + 'DECIDE: keep\n' * 6000
                + '<!-- /foragent:risk-notes -->\n<!-- /foragent:lane-decisions -->\n')
    fitted = _fit_keeps_locked_parts(body)
    assert 'DECIDE: keep\n' in fitted
    assert fit_pr_body(fitted) == fitted


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_tracker_writes_keep_required_parts_of_oversized_risk_notes(tmp_path, operation):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, RecordingTracker

    remote = FakeTracker()
    writer = RecordingTracker(remote, SimpleNamespace(store=StateStore(tmp_path), config=Config()))
    body = _rendered('pr-body', **FLOOD['risk'])
    if operation == 'create':
        writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    else:
        pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body='Closes #7')
        writer.update_pr(pr, base='main', title='Feature', body=body)
    fitted = remote.created_prs[0].body
    retained = next((tmp_path / 'pr-bodies').iterdir())
    assert retained.read_text(encoding='utf-8') == body
    assert retained.relative_to(tmp_path).as_posix() in fitted
    fitted = fitted.replace(retained.relative_to(tmp_path).as_posix(), 'pr-bodies/original.md')
    fitted = fitted.replace("_Shortened to fit GitHub's limit; full text: ", '_full text: ')
    _assert_required_survive(fitted)


QUOTES = {
    'quoted-approval-section': '## Needs a human check\n\n' + '- [ ] quoted box\n' * 5000,
    'quoted-summaries': '## What changed\nquoted summary\n\n' * 3000,
    'distinct-sections': ''.join(f'## section {i}\n- [ ] quoted {i}\n' for i in range(4000)),
    'table-headers': ''.join(f'## section {i}\n| a | b |\n| - | - |\n\n' for i in range(3000)),
    'marker-pairs': ''.join(f'<!-- foragent:q{i} -->\nx\n<!-- /foragent:q{i} -->\n' for i in range(3000)),
    'references': ''.join(f'Closes #{i}\n' for i in range(9000)),
}


def _sent_through_tracker(tmp_path, body, operation):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, RecordingTracker

    remote = FakeTracker()
    writer = RecordingTracker(remote, SimpleNamespace(store=StateStore(tmp_path), config=Config()))
    if operation == 'create':
        writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
        return writer, remote, 1
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body='Closes #7')
    writer.update_pr(pr, base='main', title='Feature', body=body)
    return writer, remote, pr


def _as_notice(fitted, tmp_path):
    retained = next(p for p in (tmp_path / 'pr-bodies').iterdir() if p.suffix == '.md')
    path = retained.relative_to(tmp_path).as_posix()
    assert path in fitted
    return (fitted.replace(path, 'pr-bodies/original.md')
            .replace("_Shortened to fit GitHub's limit; full text: ", '_full text: '))


@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('name', QUOTES)
@pytest.mark.parametrize('slot', ['searched', 'rebuttals'])
def test_quoted_structure_cannot_cost_the_real_parts(tmp_path, name, operation, slot):
    body = _rendered('pr-body', **{slot: QUOTES[name]})
    assert len(body) > 65536
    _, remote, _ = _sent_through_tracker(tmp_path, body, operation)
    fitted = _as_notice(remote.created_prs[0].body, tmp_path)
    _assert_required_survive(fitted)
    assert 'keep risk' in fitted and 'keep decision' in fitted
    assert fitted.splitlines()[0] == 'Closes #7'


@pytest.mark.parametrize('name', QUOTES)
def test_quoted_findings_cannot_cost_the_real_parts(tmp_path, name):
    from types import SimpleNamespace

    from foragent.pr_body_limit import fit_pr_body
    from foragent.run_review import ReviewResult

    parsed = SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Human approval')])
    body = _pr_body(7, parsed, [CheckResult('real check', 0, False, '')], 1,
                    [ReviewResult('PASS', QUOTES[name])], SUMMARY_CARD + QUOTES[name])
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    _assert_required_survive(fitted)


def _refresh(root, body, notes='new risk', decisions='new decision'):
    from foragent.run_review import refreshed_native_body

    return refreshed_native_body(root, body, 7, 'tests/acceptance/', 'en', notes, decisions)


@pytest.mark.parametrize('sent', ['fits', 'was-shortened'])
@pytest.mark.parametrize('name', QUOTES)
def test_refresh_uses_the_recorded_quotes_of_the_sent_body(tmp_path, name, sent):
    """The review-only path starts from GitHub's plain text of what the gate sent."""
    from foragent.run_review import ReviewResult

    def render(quote):
        return _pr_body(7, SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Human approval')]),
                        [CheckResult('real check', 0, False, '')], 1, [ReviewResult('PASS', '')], SUMMARY_CARD,
                        review_scope='pr-body', risk_notes='old risk', lane_decisions='old decision',
                        searched=quote)

    quote = (QUOTES[name] * 2)[:80000]
    if sent == 'fits':
        quote = quote[:65536 - len(render('x')) + 1]
    body = render(quote)
    assert (len(body) == 65536) == (sent == 'fits')
    writer, remote, pr = _sent_through_tracker(tmp_path, body, 'create')
    plain = str(remote.created_prs[0].body)
    # The refresh must cost more than the blocks of a quote read as structure could give up alone.
    writer.update_pr(pr, base='main', title='Feature', body=_refresh(tmp_path, plain, 'new risk\n' + 'z' * 20000))
    fitted = remote.created_prs[0].body
    lines = fitted.splitlines()
    assert len(fitted) <= 65536
    assert lines[0] == 'Closes #7' and 'The short summary.' in lines
    assert 'VERDICT: PASS' in fitted and 'Opened by Foragent gate (attempt 1)' in lines
    assert '| <code>real check</code> | pass |' in lines
    assert re.search(r'^## Needs a human check\n\s*- \[ \] Human approval$', fitted, re.MULTILINE)
    assert 'new risk' in lines and 'z' * 20000 in lines and 'new decision' in lines
    assert 'old risk' not in lines and 'old decision' not in lines


def _exact_limit_pairs(closing_first):
    pairs = []
    size = len('Closes #7\n')
    for i in range(10000):
        pair = f'<!-- foragent:q{i} -->\nx\n<!-- /foragent:q{i} -->\n'
        if size + len(pair) > 65536:
            break
        pairs.append(pair)
        size += len(pair)
    flood = ''.join(pairs)
    return ('Closes #7\n' + flood) if closing_first else (flood + 'Closes #7\n')


@pytest.mark.parametrize('closing_first', [True, False])
def test_refresh_of_an_unrecorded_body_keeps_what_it_writes(tmp_path, closing_first):
    from foragent.pr_body_limit import fit_pr_body

    body = _exact_limit_pairs(closing_first)
    assert 65000 < len(body) <= 65536
    notes = 'new risk\n' + 'z' * 20000
    refreshed = _refresh(tmp_path, body, notes)
    fitted = fit_pr_body(refreshed, notice='_full text: pr-bodies/original.md_')
    assert len(fitted) <= 65536
    lines = fitted.splitlines()
    assert 'Closes #7' in lines and 'new risk' in lines and 'z' * 20000 in lines and 'new decision' in lines
    for marker in ('risk-notes', 'lane-decisions'):
        assert lines.count(f'<!-- foragent:{marker} -->') == lines.count(f'<!-- /foragent:{marker} -->') == 1
    assert '## Review scope' in lines
    # Everything the flood gives up, it gives up a whole pair at a time.
    opened = re.findall(r'<!-- foragent:(q\d+) -->', fitted)
    assert opened and opened == re.findall(r'<!-- /foragent:(q\d+) -->', fitted)


def test_only_a_reference_flood_outlasts_nothing_the_caller_wrote(tmp_path):
    from foragent.pr_body_limit import fit_pr_body

    flood = ''.join(f'Closes #{i}\n' for i in range(100, 7000))
    refreshed = _refresh(tmp_path, flood[:60000].rsplit('\n', 1)[0] + '\nCloses #7\n', 'new risk\n' + 'z' * 20000)
    fitted = fit_pr_body(refreshed, notice='_full text: pr-bodies/original.md_')
    assert len(fitted) <= 65536
    lines = fitted.splitlines()
    assert 'Closes #7' in lines and 'new risk' in lines and 'new decision' in lines
    assert 'z' * 20000 in lines


@pytest.mark.parametrize('field', ['notes', 'review_notes'])
def test_review_notes_are_quoted_text(field):
    from foragent.pr_body_limit import fit_pr_body
    from foragent.run_review import ReviewResult

    parsed = SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Human approval')])
    review = ReviewResult('PASS', '', **{field: (QUOTES['distinct-sections'] * 2,)})
    body = _pr_body(7, parsed, [CheckResult('real check', 0, False, '')], 1, [review], SUMMARY_CARD)
    fitted = fit_pr_body(body, notice='_full text: pr-bodies/original.md_')
    _assert_required_survive(fitted)


def test_batch_pr_records_child_titles_as_quoted(run_case):  # noqa: F811
    from runner_support import batch_setup

    from foragent.pr_body_limit import load_quoted
    from foragent.runner import run_issue

    c = run_case
    batch_setup(c)
    c.tracker.add_issue(200, title='## Needs a human check', state='closed')
    c.tracker.set_parent(200, 99)
    assert run_issue(1, c.ctx) == 'merged'
    batch = next(pr for pr in c.tracker.created_prs if pr.head == 'foragent/batch-99')
    quoted = load_quoted(c.store.root, batch.body)
    assert quoted.quoted
    assert all(quoted[start:end].startswith('- #') for start, end in quoted.quoted)
    assert any('## Needs a human check' in quoted[start:end] for start, end in quoted.quoted)


def test_refresh_edits_the_real_block_not_a_quoted_copy(tmp_path):
    from foragent.pr_body_limit import (
        PrBody,
        Quoted,
        change_outside_quoted,
        join_pr_body,
        record_quoted,
    )

    quote = '<!-- foragent:risk-notes -->\nquoted\n<!-- /foragent:risk-notes -->'
    body = join_pr_body(['Closes #7\n', Quoted(quote), '\n<!-- foragent:risk-notes -->\nreal\n<!-- /foragent:risk-notes -->'])
    record_quoted(tmp_path, body)
    refreshed = _refresh(tmp_path, str(body))
    assert quote in refreshed and '\nreal\n' not in refreshed and 'new risk' in refreshed
    assert isinstance(change_outside_quoted(PrBody('x'), str.upper), PrBody)
    assert refreshed[slice(*refreshed.quoted[0])] == quote


def test_a_recorded_body_edited_on_github_falls_back_to_its_text(tmp_path):
    from foragent.pr_body_limit import Quoted, join_pr_body, load_quoted, record_quoted

    body = join_pr_body(['Closes #7\n', Quoted('quoted')])
    record_quoted(tmp_path, body)
    assert load_quoted(tmp_path, str(body)).quoted == body.quoted
    assert load_quoted(tmp_path, str(body) + ' edited').quoted == ()


def test_batch_titles_quoting_structure_cannot_cost_the_parent_line():
    from foragent.pr_body_limit import Quoted, fit_pr_body, join_pr_body

    lines = ['Sub-issues of #99', '', '## Children', '',
             *(Quoted(f'- #{i}: Closes #{i}\n<!-- foragent:q{i} -->') for i in range(6000))]
    fitted = fit_pr_body(join_pr_body(lines, '\n'))
    assert len(fitted) <= 65536 and fitted.startswith('Sub-issues of #99\n\n## Children\n')

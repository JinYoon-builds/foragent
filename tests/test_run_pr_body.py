from datetime import UTC, datetime
import re
from types import SimpleNamespace

import pytest

from foragent.issue import parse_body
from foragent.pr_body_limit import (
    PrBody, PrBodyTooLarge, Quoted, comment_parts, fit_pr_body, join_pr_body, load_quoted, record_quoted,
)
from foragent.run_checks import CheckResult
from foragent.run_pr_body import _pr_body
from foragent.run_review import ReviewResult, refreshed_native_body
from foragent.tracker import TrackerError

from runner_support import run_case  # noqa: F401


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


LIMIT = 65536
CARD = '## What changed\nThe short summary.\n## Try it\nkind: cli\n'
APPROVAL = re.compile(r'^## Needs a human check\n\s*- \[ \] Human approval$', re.M)
PAIR_FLOOD = ''.join(f'<!-- foragent:q{i} -->\nx\n<!-- /foragent:q{i} -->\n' for i in range(1300))
QUOTES = {
    'approval': '## Needs a human check\n\n' + '- [ ] quoted box\n' * 5000,
    'summaries': '## What changed\nquoted summary\n\n' * 3000,
    'sections': ''.join(f'## section {i}\n- [ ] quoted {i}\n' for i in range(4000)),
    'tables': ''.join(f'## section {i}\n| a | b |\n| - | - |\n\n' for i in range(3000)),
    'pairs': PAIR_FLOOD * 2,
    'references': ''.join(f'Closes #{i}\n' for i in range(9000)),
    'verdicts': 'VERDICT: FAIL\n' * 6000,
}


def _too_much_required(n=7):
    """A body the gate rendered, so its approval lines are known to be real."""
    return join_pr_body([f'Closes #{n}\n', Quoted('quoted', 'Searched'), '\n## Needs a human check\n', '- [ ] box\n' * 8000])


def _note(label):
    return f'_{label}: removed. full text: pr-bodies/original.md_'


def _rendered(*, checks=None, reviews=None, try_text=CARD, **fields):
    parsed = SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Human approval')])
    return _pr_body(7, parsed, checks or [CheckResult('real check', 0, False, '')], 1,
                    reviews or [ReviewResult('PASS', '')], try_text,
                    **{'review_scope': 'pr-body', 'risk_notes': 'keep risk', 'lane_decisions': 'keep decision', **fields})


def _writer(tmp_path):
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, RecordingTracker

    remote = FakeTracker()
    context = SimpleNamespace(store=StateStore(tmp_path), config=Config(), now=lambda: datetime.now(UTC))
    return RecordingTracker(remote, context), remote


def _assert_required(fitted):
    assert len(fitted) <= LIMIT
    lines = fitted.splitlines()
    assert lines[0] == 'Closes #7'
    assert 'The short summary.' in lines
    assert 'VERDICT: PASS' in lines
    assert APPROVAL.search(fitted)
    assert 'Opened by Foragent gate (attempt 1)' in lines
    for name in ('risk-notes', 'lane-decisions'):
        assert lines.count(f'<!-- foragent:{name} -->') == lines.count(f'<!-- /foragent:{name} -->') == 1
        assert lines.index(f'<!-- foragent:{name} -->') < lines.index(f'<!-- /foragent:{name} -->')


@pytest.mark.parametrize('flood,removed', [
    ({'risk_notes': 'risk\n' * 20000}, 'risk-notes'),
    ({'lane_decisions': 'DECIDE: keep\n' * 6000}, 'lane-decisions'),
    ({'searched': 'e\n' * 40000}, 'Searched'),
    ({'rebuttals': 'e\n' * 40000}, 'Rebuttals'),
    ({'try_text': CARD + 'e\n' * 40000}, 'Try card'),
    ({'checks': [CheckResult(f'check {i}', 0, False, '') for i in range(4000)]}, 'Done when table'),
], ids=['risk', 'decisions', 'searched', 'rebuttals', 'try', 'table'])
def test_one_oversized_block_becomes_a_note_and_leaves_the_rest(flood, removed):
    body = _rendered(**flood)
    assert len(body) > LIMIT
    fitted = fit_pr_body(body, _note)
    _assert_required(fitted)
    assert fitted.count('removed. full text') == 1 and f'_{removed}: removed' in fitted
    for block, kept in (('risk-notes', 'keep risk'), ('lane-decisions', 'keep decision'),
                        ('Done when table', '| <code>real check</code> | pass |')):
        assert kept in fitted or block == removed


@pytest.mark.parametrize('review,label', [
    (ReviewResult('PASS', 'e\n' * 40000), 'Round 1 findings'),
    (ReviewResult('PASS', '', review_notes=('e\n' * 40000,)), 'Round 1 notes'),
    (ReviewResult('PASS', '', notes=('e\n' * 40000,)), 'Round 1 notes'),
], ids=['findings', 'review-notes', 'notes'])
def test_review_text_in_a_local_review_is_one_optional_block(review, label):
    fitted = fit_pr_body(_rendered(reviews=[review], review_scope='claude-local'), _note)
    assert len(fitted) <= LIMIT and fitted.splitlines()[0] == 'Closes #7'
    assert 'Round 1: VERDICT: PASS' in fitted and APPROVAL.search(fitted) and 'The short summary.' in fitted
    assert f'_{label}: removed' in fitted


@pytest.mark.parametrize('quote', QUOTES)
def test_quoted_review_text_cannot_become_required(quote):
    review = ReviewResult('PASS', QUOTES[quote])
    fitted = fit_pr_body(_rendered(reviews=[review], review_scope='claude-local'), _note)
    assert fitted.splitlines()[0] == 'Closes #7' and APPROVAL.search(fitted)
    assert 'Round 1: VERDICT: PASS' in fitted and 'Opened by Foragent gate (attempt 1)' in fitted


@pytest.mark.parametrize('size', [20000, 30000, 70000, 700000, 7000000])
def test_a_block_size_never_changes_another_block(size):
    """The sent text of everything else is the same however large the flood grows."""
    def fitted(risk):
        out = fit_pr_body(_rendered(risk_notes='r' * risk, searched='s' * 70000, rebuttals='b' * 9000), _note)
        return re.sub(r'r{5,}|_risk-notes: removed[^\n]*', 'RISK', out)

    assert fitted(size) == fitted(90000)
    assert 'b' * 9000 in fitted(size) and '_Searched: removed' in fitted(size)


def test_the_held_sizes_keep_or_remove_rebuttals_the_same_way():
    """Searched 70,000 and rebuttals 40,000: risk notes 20,000 and 30,000 cannot differ on rebuttals."""
    fates = {('b' * 40000 in fit_pr_body(_rendered(risk_notes='r' * risk, searched='s' * 70000, rebuttals='b' * 40000),
                                         _note))
             for risk in (20000, 30000)}
    assert len(fates) == 1


def test_a_block_within_its_share_stays_for_any_number_of_oversized_neighbours():
    body = join_pr_body(['Closes #7\n', Quoted('kept', 'Small'), '\n',
                         *(part for i in range(5) for part in (Quoted('e' * 70000, f'Big {i}'), '\n'))])
    fitted = fit_pr_body(body, _note)
    assert '\nkept\n' in fitted and all(f'_Big {i}: removed' in fitted for i in range(5))
    assert len(fitted) <= LIMIT


def test_blocks_are_whole_or_a_note_never_part_of_one():
    table = [CheckResult(f'check {i}', 0, False, '') for i in range(4000)]
    fitted = fit_pr_body(_rendered(checks=table), _note)
    assert '| <code>check 0</code>' not in fitted and 'Done when table: removed' in fitted
    evidence = 'first\n' + 'e' * 70000 + '\nlast'
    fitted = fit_pr_body(_rendered(searched=evidence), _note)
    assert 'first' not in fitted and 'Searched: removed' in fitted


@pytest.mark.parametrize('name', QUOTES)
@pytest.mark.parametrize('slot', ['searched', 'rebuttals', 'try_text'])
def test_quoted_text_cannot_become_required(name, slot):
    quote = (CARD + QUOTES[name]) if slot == 'try_text' else QUOTES[name]
    fitted = fit_pr_body(_rendered(**{slot: quote}), _note)
    _assert_required(fitted)
    assert 'keep risk' in fitted and 'keep decision' in fitted


@pytest.mark.parametrize('kind', ['fits', 'one-over'])
def test_the_limit_is_exact_and_a_fitting_body_is_returned_verbatim(kind):
    size = LIMIT if kind == 'fits' else LIMIT + 1
    body = 'Closes #7\n' + '증' * (size - len('Closes #7\n'))
    fitted = fit_pr_body(body, _note)
    assert (fitted == body) == (kind == 'fits')
    assert len(fitted) <= LIMIT and fitted.startswith('Closes #7\n')


def test_required_content_over_the_limit_refuses_instead_of_cutting():
    with pytest.raises(PrBodyTooLarge):
        fit_pr_body(_too_much_required(), _note)


def test_notes_that_do_not_fit_refuse_instead_of_passing_the_limit():
    required = 'Closes #7\n## Needs a human check\n' + '- [ ] b\n' * 8100
    assert len(required) <= LIMIT
    body = join_pr_body([required, Quoted('e' * 1000, 'Searched')])
    assert len(body) > LIMIT
    room = LIMIT - len(required)
    with pytest.raises(PrBodyTooLarge):
        fit_pr_body(body, lambda label: 'n' * (room + 1))
    assert len(fit_pr_body(body, lambda label: 'n')) <= LIMIT


def test_a_summary_too_long_to_be_short_is_prose_and_cannot_stop_the_pr():
    long_card = '## What changed\n' + 's' * 70000 + '\n'
    fitted = fit_pr_body(_rendered(try_text=long_card), _note)
    assert len(fitted) <= LIMIT and 's' * 70000 not in fitted


def test_oversized_writes_keep_the_full_text_in_comments_and_a_file(tmp_path):
    writer, remote = _writer(tmp_path)
    body = _rendered(searched='증거\n' * 60000)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    sent = remote.created_prs[0].body
    _assert_required(sent)
    comments = remote.list_comments(pr)
    assert len(comments) > 1 and all(len(comment.body) <= LIMIT for comment in comments)
    assert ''.join(comment.body.split('\n\n', 1)[1] for comment in comments) == str(body)
    header = comments[0].body.split(' (1/')[0]
    assert f'"{header}"' in sent
    retained = next(tmp_path.glob('pr-bodies/*.md'))
    assert retained.read_text(encoding='utf-8') == str(body)
    assert retained.relative_to(tmp_path).as_posix() in sent


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_writes_refuse_before_sending_anything(tmp_path, operation):
    writer, remote = _writer(tmp_path)
    body = _too_much_required()
    pr = None
    if operation == 'update':
        pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body='Closes #7')
    with pytest.raises(PrBodyTooLarge):
        if operation == 'update':
            writer.update_pr(pr, base='main', title='Feature', body=body)
        else:
            writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    assert not list(tmp_path.glob('pr-bodies/*.md'))
    if operation == 'update':
        assert remote.created_prs[0].body == 'Closes #7' and not remote.list_comments(pr)
    else:
        assert not remote.created_prs


def test_the_same_oversized_body_posts_its_comments_once_and_fits_a_later_send(tmp_path):
    writer, remote = _writer(tmp_path)
    body = _rendered(searched='e\n' * 40000)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    count = len(remote.list_comments(pr))
    assert count
    writer.update_pr(pr, base='main', title='Feature', body=body)
    assert len(remote.list_comments(pr)) == count
    writer.update_pr(pr, base='main', title='Feature', body=_rendered(searched='f\n' * 40000))
    assert len(remote.list_comments(pr)) > count


@pytest.mark.parametrize('message,raises', [('comment rejected', False), ('API rate limit exceeded', True)])
def test_a_failed_full_text_comment_keeps_the_pr_and_the_file(tmp_path, monkeypatch, message, raises):
    writer, remote = _writer(tmp_path)

    def refuse(*_args, **_kwargs):
        raise TrackerError(message)

    monkeypatch.setattr(remote, 'comment', refuse)
    body = _rendered(searched='e\n' * 40000)
    if raises:
        with pytest.raises(TrackerError):
            writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    else:
        writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    assert len(remote.created_prs) == 1
    assert next(tmp_path.glob('pr-bodies/*.md')).read_text(encoding='utf-8') == str(body)


def test_comment_parts_fit_and_rebuild():
    body = '증' * 200000
    parts = comment_parts(body, 'Full PR body abc')
    assert all(len(part) <= 65536 for part in parts) and parts[0].startswith('Full PR body abc (1/')
    assert ''.join(part.split('\n\n', 1)[1] for part in parts) == body


def _refresh(root, body, notes='new risk', decisions='new decision', pr=1):
    return refreshed_native_body(root, pr, body, 'tests/acceptance/', 'en', notes, decisions)


@pytest.mark.parametrize('recorded', [True, False])
@pytest.mark.parametrize('flood', [
    {'notes': 'risk\n' * 20000}, {'decisions': 'DECIDE: keep\n' * 6000},
], ids=['risk', 'decisions'])
def test_refresh_with_oversized_notes_keeps_the_required_parts(tmp_path, recorded, flood):
    """The held case: a normal PR refreshed with notes that alone pass the limit."""
    body = _rendered()
    if recorded:
        record_quoted(tmp_path, 1, body)
    fitted = fit_pr_body(_refresh(tmp_path, str(body), **flood), _note)
    _assert_required(fitted)
    assert 'removed. full text' in fitted


@pytest.mark.parametrize('name', ['approval', 'summaries', 'sections', 'tables', 'references', 'verdicts'])
@pytest.mark.parametrize('sent', ['fits', 'was-shortened'])
def test_refresh_reads_the_recorded_quotes_of_what_was_sent(tmp_path, name, sent):
    quote = (QUOTES[name] * 2)[:80000]
    if sent == 'fits':
        quote = quote[:LIMIT - len(_rendered(searched='x')) + 1]
    writer, remote = _writer(tmp_path)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=_rendered(searched=quote))
    plain = str(remote.created_prs[0].body)
    assert (len(plain) == LIMIT) == (sent == 'fits')
    writer.update_pr(pr, base='main', title='Feature', body=_refresh(tmp_path, plain, pr=pr))
    fitted = remote.created_prs[0].body
    _assert_required(fitted)
    lines = fitted.splitlines()
    assert 'new risk' in lines and 'new decision' in lines and 'keep risk' not in lines


def test_refresh_of_an_unrecorded_marker_flood_keeps_every_marker_and_the_required_parts(tmp_path):
    head = 'Closes #7\n\n## What changed\nThe short summary.\n\n## Review\n\nVERDICT: PASS\n\n'
    tail = '## Needs a human check\n\n- [ ] Human approval\n\nOpened by Foragent gate (attempt 1)\n'
    flood = ''.join(f'<!-- foragent:q{i} -->\nx\n<!-- /foragent:q{i} -->\n' for i in range(1200))
    body = head + flood + tail
    assert len(body) <= LIMIT
    fitted = fit_pr_body(_refresh(tmp_path, body, notes='new risk\n' + 'z' * 20000), _note)
    _assert_required(fitted)
    assert re.findall(r'<!-- foragent:(q\d+) -->', fitted) == [f'q{i}' for i in range(1200)]
    assert re.findall(r'<!-- /foragent:(q\d+) -->', fitted) == [f'q{i}' for i in range(1200)]


def test_a_later_refresh_finds_a_pair_whose_text_became_a_note(tmp_path):
    first = fit_pr_body(_rendered(risk_notes='r' * 70000), _note)
    assert 'risk-notes: removed' in first
    refreshed = _refresh(tmp_path, str(first))
    assert 'new risk' in refreshed.splitlines() and 'risk-notes: removed' not in refreshed
    assert fit_pr_body(refreshed, _note) == refreshed


def test_nested_pairs_keep_both_markers_when_their_text_is_removed():
    inner = '<!-- foragent:lane-decisions -->\n' + 'DECIDE: keep\n' * 6000 + '<!-- /foragent:lane-decisions -->\n'
    body = f'Closes #7\n<!-- foragent:risk-notes -->\nrisk\n{inner}<!-- /foragent:risk-notes -->\n'
    lines = fit_pr_body(body, _note).splitlines()
    for name in ('risk-notes', 'lane-decisions'):
        assert lines.count(f'<!-- foragent:{name} -->') == lines.count(f'<!-- /foragent:{name} -->') == 1
    assert lines[0] == 'Closes #7'


def test_refresh_edits_the_real_block_not_a_quoted_copy(tmp_path):
    quote = '<!-- foragent:risk-notes -->\nquoted\n<!-- /foragent:risk-notes -->'
    body = join_pr_body(['Closes #7\n', Quoted(quote, 'Searched'),
                         '\n<!-- foragent:risk-notes -->\nreal\n<!-- /foragent:risk-notes -->'])
    record_quoted(tmp_path, 1, body)
    refreshed = _refresh(tmp_path, str(body))
    assert quote in refreshed and '\nreal\n' not in refreshed and 'new risk' in refreshed
    start, end, label = refreshed.quoted[0]
    assert refreshed[start:end] == quote and label == 'Searched'


def test_quotes_are_found_again_after_an_edit_elsewhere_on_github(tmp_path):
    body = join_pr_body(['Closes #7\n', Quoted('quoted', 'Searched'), '\nreal\n', Quoted('second', 'Rebuttals')])
    record_quoted(tmp_path, 7, body)
    assert load_quoted(tmp_path, 7, str(body)).quoted == body.quoted
    edited = load_quoted(tmp_path, 7, 'Edited above\n' + str(body) + ' ')
    assert [(edited[start:end], label) for start, end, label in edited.quoted] == [
        ('quoted', 'Searched'), ('second', 'Rebuttals')]
    without = load_quoted(tmp_path, 7, str(body).replace('quoted', 'changed'))
    assert [label for _, _, label in without.quoted] == ['Rebuttals']
    assert load_quoted(tmp_path, 8, str(body)).quoted == ()
    assert isinstance(load_quoted(tmp_path, 8, 'unknown'), PrBody)


def test_a_plain_resend_keeps_the_record_of_a_rendered_body(tmp_path):
    writer, remote = _writer(tmp_path)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=_rendered(searched='small'))
    plain = str(remote.created_prs[0].body)
    writer.update_pr(pr, base='main', title='Retitled', body=plain)
    assert [label for _, _, label in load_quoted(tmp_path, pr, plain).quoted if label == 'Searched'] == ['Searched']


def test_kept_quotes_are_still_located_after_fitting():
    body = _rendered(searched='small evidence', risk_notes='r' * 70000)
    fitted = fit_pr_body(body, _note)
    spans = {label: fitted[start:end] for start, end, label in fitted.quoted}
    assert spans['Searched'] == 'small evidence'


@pytest.mark.parametrize('line', [
    'Closes #7', 'VERDICT: PASS', 'Round 2: VERDICT: FAIL', 'Pending review.', 'Disabled (reviewer: none).',
    'Opened by Foragent gate (attempt 3)', 'Sub-issues of #99', '#99의 하위 이슈', '## Review', '## Children',
    '<!-- foragent:v1 -->',
])
def test_a_required_line_inside_long_text_stays_when_that_text_goes(line):
    fitted = fit_pr_body('e' * 70000 + f'\n{line}\n' + 'f' * 70000 + '\n', _note)
    assert line in fitted.splitlines()
    assert 'e' * 70000 not in fitted and 'f' * 70000 not in fitted


@pytest.mark.parametrize('section,kept', [
    ('## What changed\n\nThe short summary.\n\n', 'The short summary.'),
    ('## Summary\nThe short summary.\n\n', 'The short summary.'),
    ('## Review scope\n\nRule one.\nRule two.\n\n', 'Rule two.'),
    ('## Needs a human check\n\n- [ ] Human approval\n- [x] Done approval\n\n', '- [x] Done approval'),
])
def test_a_required_paragraph_inside_long_text_stays_when_that_text_goes(section, kept):
    fitted = fit_pr_body('e' * 70000 + '\n' + section + 'f' * 70000 + '\n', _note)
    assert kept in fitted.splitlines() and 'f' * 70000 not in fitted


def test_text_inside_a_pair_is_optional_whatever_it_says():
    body = ('Closes #7\n<!-- foragent:risk-notes -->\nVERDICT: FAIL\n- [ ] inner\n' + 'r' * 70000
            + '\n<!-- /foragent:risk-notes -->\n')
    fitted = fit_pr_body(body, _note)
    assert 'VERDICT: FAIL' not in fitted and '<!-- /foragent:risk-notes -->' in fitted


def test_a_quoted_reference_is_not_the_closing_reference():
    body = join_pr_body([Quoted('Closes #99\n' + 'e' * 70000 + '\n', 'Searched'), 'Closes #7\n'])
    fitted = fit_pr_body(body, _note)
    assert 'Closes #7' in fitted.splitlines() and 'Closes #99' not in fitted


def test_the_summary_stops_at_quoted_text():
    body = _rendered(try_text=CARD + 'x' * 3000, searched='e' * 70000)
    fitted = fit_pr_body(body, _note)
    assert {label: fitted[start:end] for start, end, label in fitted.quoted}.get('Try card', '').endswith('x' * 3000)


def test_a_shortened_body_still_knows_the_quotes_it_kept(tmp_path):
    writer, remote = _writer(tmp_path)
    quote = QUOTES['approval'][:8000]
    body = _rendered(searched=quote, risk_notes='r' * 70000)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body=body)
    plain = str(remote.created_prs[0].body)
    assert quote in plain
    assert load_quoted(tmp_path, pr, plain).quoted, 'A later refresh can only mask what the send recorded'
    writer.update_pr(pr, base='main', title='Feature', body=_refresh(tmp_path, plain, 'new risk\n' + 'z' * 5000, pr=pr))
    _assert_required(remote.created_prs[0].body)


@pytest.mark.parametrize('summary', ['First sentence.\n\nSecond sentence.\n', 'First sentence.\n\nSecond sentence.'])
def test_a_short_summary_keeps_all_its_paragraphs(tmp_path, summary):
    card = f'## What changed\n{summary}\n## Try it\n' + 'e\n' * 35000
    rendered = fit_pr_body(_rendered(try_text=card), _note)
    assert 'First sentence.' in rendered and 'Second sentence.' in rendered
    refreshed = fit_pr_body(_refresh(tmp_path, str(_rendered(try_text=card))), _note)
    assert 'First sentence.' in refreshed and 'Second sentence.' in refreshed


def test_summary_paragraphs_past_the_short_limit_are_prose():
    card = '## What changed\nFirst sentence.\n\n' + 's' * 70000 + '\n'
    fitted = fit_pr_body(_rendered(try_text=card), _note)
    assert 'First sentence.' in fitted and 's' * 70000 not in fitted


@pytest.mark.parametrize('recorded', [True, False])
@pytest.mark.parametrize('pair', ['risk-notes', 'lane-decisions'])
@pytest.mark.parametrize('flood', ['- [ ] checklist line\n' * 4000, 'VERDICT: FAIL\n' * 6000])
def test_pair_text_under_the_final_approval_section_stays_optional(tmp_path, recorded, pair, flood):
    head = 'Closes #7\n\n## Review scope\n\nRule.\n\n## Needs a human check\n\n- [ ] Human approval\n\n'
    block = f'<!-- foragent:{pair} -->\n{flood}<!-- /foragent:{pair} -->\n'
    body = join_pr_body([head, block, Quoted('quoted', 'Searched')]) if recorded else head + block
    fitted = fit_pr_body(body, _note)
    assert '- [ ] Human approval' in fitted.splitlines() and flood not in fitted and len(fitted) <= LIMIT


def test_a_refresh_appended_under_the_approval_section_stays_optional(tmp_path):
    body = 'Closes #7\n\n## Review scope\n\nRule.\n\n## Needs a human check\n\n- [ ] Human approval\n'
    fitted = fit_pr_body(_refresh(tmp_path, body, '- [ ] checklist\n' * 6000), _note)
    assert '- [ ] Human approval' in fitted.splitlines() and '- [ ] checklist' not in fitted


def test_a_section_stops_at_a_marker():
    body = 'Closes #7\n## Summary\nShort.\n<!-- foragent:risk-notes -->\n' + 'r' * 3000 + '\n<!-- /foragent:risk-notes -->\n'
    fitted = fit_pr_body(body, _note, limit=2000)
    assert 'Short.' in fitted.splitlines() and 'r' * 3000 not in fitted


def test_a_block_shorter_than_its_note_stays_past_its_share():
    body = join_pr_body(['Closes #7\n', Quoted('e' * 110, 'One'), '\n', Quoted('t', 'Two'), '\n', Quoted('u', 'Three'), '\n',
                         Quoted('g' * 400, 'Big'), '\n'])
    fitted = fit_pr_body(body, lambda label: 'n' * 120, limit=260)
    assert 'e' * 110 in fitted and 'g' * 400 not in fitted and len(fitted) <= 260


def test_every_recorded_verdict_stays_even_inside_long_text():
    body = join_pr_body(['Closes #7\n', Quoted('quoted', 'Searched'), '\nRound 1: VERDICT: FAIL\n', 'e' * 70000,
                         '\nRound 2: VERDICT: FAIL\n', 'f' * 70000, '\nRound 3: VERDICT: PASS\n'])
    lines = fit_pr_body(body, _note).splitlines()
    for line in ('Round 1: VERDICT: FAIL', 'Round 2: VERDICT: FAIL', 'Round 3: VERDICT: PASS'):
        assert line in lines


def test_all_review_notes_of_a_round_are_one_block():
    review = ReviewResult('PASS', '', review_notes=('a' * 30000, 'b' * 30000), notes=('c' * 30000,))
    fitted = fit_pr_body(_rendered(reviews=[review], review_scope='claude-local'), _note)
    assert fitted.count('_Round 1 notes: removed') == 1


@pytest.mark.parametrize('edit', [lambda body: body + ' ', lambda body: 'Edited by a person.\n' + body],
                         ids=['trailing-space', 'line-above'])
@pytest.mark.parametrize('name', QUOTES)
def test_a_body_edited_on_github_keeps_its_quotes_optional(tmp_path, edit, name):
    '''The held case: a fitting body full of quotes, one edit on GitHub, then a normal refresh.'''
    writer, remote = _writer(tmp_path)
    quote = (QUOTES[name] * 2)[:64000]
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature',
                          body=_rendered(searched=quote[:LIMIT - len(_rendered(searched='x')) - 40]))
    plain = edit(str(remote.created_prs[0].body))
    writer.update_pr(pr, base='main', title='Feature', body=_refresh(tmp_path, plain, pr=pr))
    fitted = remote.created_prs[0].body
    assert len(fitted) <= LIMIT and 'Closes #7' in fitted.splitlines()
    assert APPROVAL.search(fitted) and 'The short summary.' in fitted.splitlines()
    assert 'VERDICT: PASS' in fitted.splitlines() and 'new risk' in fitted.splitlines()


OLDER_HEAD = 'Closes #7\n\n## What changed\nThe short summary.\n\n## Review\n\n'
OLDER_TAIL = 'Opened by Foragent gate (attempt 1)\n'


def _older(*lines, text='e' * 70000):
    return OLDER_HEAD + '\n'.join(lines) + '\n\n## Searched\n\n' + text + '\n\n' + OLDER_TAIL


def test_an_older_body_keeps_every_real_approval_line(tmp_path):
    boxes = [f'- [ ] approval {i}' for i in range(400)]
    body = _older('## Needs a human check\n', *boxes)
    lines = fit_pr_body(_refresh(tmp_path, body), _note).splitlines()
    assert all(box in lines for box in boxes) and '## Needs a human check' in lines


def test_an_older_body_keeps_more_approval_sections_than_a_few(tmp_path):
    sections = [line for i in range(6) for line in ('## Needs a human check', f'- [ ] section {i}')]
    lines = fit_pr_body(_refresh(tmp_path, _older(*sections)), _note).splitlines()
    assert all(f'- [ ] section {i}' in lines for i in range(6))


def test_approval_lines_read_the_way_the_inbox_reads_them():
    from foragent.inspect_data import _human_checks

    long = 'e' * 70000
    body = '\n'.join(['Closes #7', long, '### Needs a human check', '* [ ] star box', long, '#### Under it', long,
                      '  + [ ] nested box', long, '## Other', '- [ ] not an approval', long, ''])
    fitted = fit_pr_body(body, _note)
    assert len(fitted) <= LIMIT and long not in fitted
    assert _human_checks(fitted).splitlines() == ['* [ ] star box', '+ [ ] nested box']


def test_an_approval_heading_inside_a_pair_is_pair_text():
    body = ('Closes #7\n<!-- foragent:risk-notes -->\n## Needs a human check\n- [ ] inner\n' + 'r' * 70000
            + '\n<!-- /foragent:risk-notes -->\n')
    fitted = fit_pr_body(body, _note)
    assert 'Needs a human check' not in fitted and '- [ ] inner' not in fitted


def test_an_update_records_the_quotes_it_sent(tmp_path):
    writer, remote = _writer(tmp_path)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body='Closes #7')
    writer.update_pr(pr, base='main', title='Feature', body=_rendered(searched='small'))
    known = load_quoted(tmp_path, pr, str(remote.created_prs[0].body))
    assert 'Searched' in [label for _, _, label in known.quoted]


def test_an_older_body_keeps_every_verdict_of_a_real_review(tmp_path):
    body = _older('Round 1: VERDICT: FAIL', '', 'f' * 70000, '', 'Round 2: VERDICT: FAIL', '', 'g' * 70000, '', 'Round 3: VERDICT: PASS')
    lines = fit_pr_body(_refresh(tmp_path, body), _note).splitlines()
    for line in ('Round 1: VERDICT: FAIL', 'Round 2: VERDICT: FAIL', 'Round 3: VERDICT: PASS'):
        assert line in lines


def test_an_older_body_keeps_a_summary_among_quoted_copies(tmp_path):
    quoted = '## What changed\nquoted summary\n\n'
    body = quoted + 'e' * 70000 + '\n## What changed\nThe real summary.\n\n' + 'f' * 70000 + '\n' + quoted
    lines = fit_pr_body(_refresh(tmp_path, 'Closes #7\n' + body), _note).splitlines()
    assert 'The real summary.' in lines


@pytest.mark.parametrize('line', ['- [ ] box\n', 'Round 1: VERDICT: PASS\n', 'Opened by Foragent gate (attempt 1)\n'])
def test_required_looking_text_past_the_limit_refuses_when_nothing_tells_it_apart(tmp_path, line):
    '''Without a record a quote and a real line look the same, and required content is never cut.'''
    body = 'Closes #7\n## Needs a human check\n' + line * (70000 // len(line) + 1)
    with pytest.raises(PrBodyTooLarge):
        fit_pr_body(body, _note)
    writer, remote = _writer(tmp_path)
    pr = writer.create_pr(head='foragent/issue-7', base='main', title='Feature', body='Closes #7')
    with pytest.raises(PrBodyTooLarge):
        writer.update_pr(pr, base='main', title='Feature', body=_refresh(tmp_path, body, pr=pr))
    assert remote.created_prs[0].body == 'Closes #7'


def test_required_too_large_holds_the_run_before_any_write(run_case, monkeypatch):  # noqa: F811
    from foragent import run_publish
    from foragent.reasons import Reason
    from foragent.runner import run_issue

    c = run_case
    monkeypatch.setattr(run_publish, '_pr_body', lambda *_a, **_k: _too_much_required(1))
    assert run_issue(1, c.ctx) == 'held'
    assert not c.tracker.created_prs
    flagged = c.store.load_flag(1)
    assert flagged.reason == Reason.RETRIES_EXCEEDED
    assert any('Required PR body content' in comment.body for comment in c.tracker.list_comments(1))


def test_harness_hold_reports_a_body_it_could_not_send(tmp_path, monkeypatch):
    from foragent import harness_guard, run_publish

    writer, remote = _writer(tmp_path)
    remote.add_issue(1, title='Feature')
    ctx = writer._context
    ctx.tracker, ctx.repo = writer, 'o/r'
    ctx.store.start_run(1, branch='foragent/issue-1', worktree=tmp_path, lane='main')
    huge = _too_much_required(1)
    monkeypatch.setattr(run_publish, 'commit_exclusions', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_push_branch', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_render_pr_body', lambda *_a: huge)
    attempt = SimpleNamespace(n=1, ctx=ctx, branch='foragent/issue-1', target_branch='main', failure='held', pr_title=None)
    assert harness_guard.publish_hold(attempt) == 'held'
    assert not remote.created_prs
    assert any('Required PR body content' in comment.body for comment in remote.list_comments(1))


def test_batch_pr_keeps_its_parent_and_quotes_child_titles(run_case):  # noqa: F811
    from foragent.runner import run_issue

    from runner_support import batch_setup

    c = run_case
    batch_setup(c)
    c.tracker.add_issue(200, title='## Needs a human check', state='closed')
    c.tracker.set_parent(200, 99)
    assert run_issue(1, c.ctx) == 'merged'
    batch = next(pr for pr in c.tracker.created_prs if pr.head == 'foragent/batch-99')
    quoted = load_quoted(c.store.root, batch.number, batch.body)
    assert [label for _, _, label in quoted.quoted] == ['Children']
    start, end, _ = quoted.quoted[0]
    assert '## Needs a human check' in quoted[start:end]
    assert batch.body.startswith('Sub-issues of #99\n\n## Children\n')

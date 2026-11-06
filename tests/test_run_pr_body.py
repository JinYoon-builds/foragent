import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from runner_support import enable_reviewer, run_case  # noqa: F401

from foragent.issue import parse_body
from foragent.run_attempt import ExecutorStopped
from foragent.run_checks import CheckResult
from foragent.run_pr_body import (
    COMMENT_CHUNK,
    LIMIT,
    Block,
    _pr_body,
    compose,
    load_values,
    merge_retitle_body,
    publish_comments,
    read_values,
    save_values,
)
from foragent.run_review import ReviewResult
from foragent.tracker import FakeTracker, TrackerError


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
    from foragent.run_publish_pr import _searched

    attempt = SimpleNamespace(worktree=tmp_path)
    assert _searched(attempt) == ''
    (tmp_path / '.foragent').mkdir()
    (tmp_path / '.foragent/SEARCHED.md').write_text('- grep writers: 3 found\n', encoding='utf-8')
    assert _searched(attempt) == '- grep writers: 3 found\n'


REQUIRED = 'Closes #7\n\n## Summary\n\nShort.'


@pytest.mark.parametrize('size', [LIMIT - len(REQUIRED) - 1, LIMIT - len(REQUIRED) - 2])
def test_a_body_that_fits_is_returned_whole_up_to_the_limit(size):
    composed = compose([REQUIRED, Block('evidence', '증' * size)])
    assert composed == REQUIRED + '\n' + '증' * size and composed.comments == []


def test_a_body_one_character_over_replaces_only_blocks_past_their_share():
    size = LIMIT - len(REQUIRED)
    composed = compose([REQUIRED, Block('evidence', 'e' * size), Block('small', 'keep')])
    assert len(composed) <= LIMIT and 'keep' in composed and 'e' * 100 not in composed
    assert 'Removed evidence; full text: PR comment `Foragent full evidence ' in composed
    assert [name.rsplit(' ', 1)[0] for name, _ in composed.comments] == ['Foragent full evidence']
    assert composed.comments[0][1] == 'e' * size


def _other_blocks(block_size):
    return compose([REQUIRED, Block('grows', 'g' * block_size), Block('huge', 'h' * 70000), Block('steady', 's' * 40000),
                    Block('small', 'keep')])


@pytest.mark.parametrize('size', [20000, 30000, 70000, 700000])
def test_a_block_length_never_changes_what_another_block_gets(size):
    composed = _other_blocks(size)
    lines = composed.splitlines()
    assert 'keep' in lines and len(composed) <= LIMIT
    # The steady block's fate is the same at every size of its neighbours.
    assert ('s' * 40000 in composed) == ('s' * 40000 in _other_blocks(20000))
    assert ('Removed steady' in composed) == ('Removed steady' in _other_blocks(700000))
    assert ('Removed huge' in composed) and 'Removed steady' in composed


def test_every_block_within_its_share_stays_for_any_neighbours():
    blocks = [Block(f'block {i}', 'x' * 16000) for i in range(3)]
    composed = compose([REQUIRED, *blocks, Block('huge', 'h' * 5000000)])
    assert all(block.text in composed for block in blocks) and 'Removed huge' in composed and len(composed) <= LIMIT


def test_required_content_past_the_limit_stops_and_reports_size_and_limit():
    required = 'r' * (LIMIT + 1)
    with pytest.raises(ExecutorStopped) as stopped:
        compose([required])
    assert stopped.value.outcome == 'pr-body-too-long'
    assert str(LIMIT + 1) in str(stopped.value) and str(LIMIT) in str(stopped.value)


def test_omission_notes_count_before_anything_can_be_sent():
    block = Block('evidence', 'e' * 70000)
    required = 'r' * (LIMIT - len(block.note))  # One separator more than fits.
    with pytest.raises(ExecutorStopped):
        compose([required, block])
    composed = compose(['r' * (LIMIT - len(block.note) - 1), block])
    assert len(composed) == LIMIT and block.note in composed


def test_required_values_with_markdown_look_stay_exact():
    summary = 'First line.\r\n\r\n## Needs a human check ##\r\nCloses #99'
    parsed = SimpleNamespace(done_when_checklist=[SimpleNamespace(text='Looks right')])
    body = _pr_body(7, parsed, [], 1, [ReviewResult('PASS', '')], 'e' * 70000, summary=summary, approval_line='Approval: a at b')
    assert body.startswith(f'Closes #7\n\n## Summary\n\n{summary}\n\nApproval: a at b')
    assert '- [ ] Looks right' in body and 'Round 1: VERDICT: PASS' in body and 'e' * 100 not in body


def _ctx(tmp_path, tracker=None):
    from foragent.state import StateStore

    return SimpleNamespace(tracker=tracker or FakeTracker(), store=StateStore(tmp_path), now=lambda: datetime.now(UTC))


def test_comments_hold_the_whole_text_in_named_chunks_and_are_posted_once(tmp_path):
    ctx = _ctx(tmp_path)
    text = '증\r\n' * (COMMENT_CHUNK)
    body = compose([REQUIRED, Block('evidence', text)])
    publish_comments(ctx, 9, body)
    publish_comments(ctx, 9, body)
    comments = ctx.tracker.list_comments(9)
    assert len(comments) == 3 and all(len(comment.body) <= 65536 for comment in comments)
    assert ''.join(comment.body.split('\n', 1)[1] for comment in comments) == text
    assert all(comment.body.startswith(body.comments[0][0]) for comment in comments)
    assert body.comments[0][0] in body


def test_changed_text_gets_its_own_comments(tmp_path):
    ctx = _ctx(tmp_path)
    publish_comments(ctx, 9, compose([REQUIRED, Block('evidence', 'e' * 70000)]))
    publish_comments(ctx, 9, compose([REQUIRED, Block('evidence', 'f' * 70000)]))
    assert {comment.body.split('\n', 1)[0] for comment in ctx.tracker.list_comments(9)} == {
        compose([REQUIRED, Block('evidence', text * 70000)]).comments[0][0] + ' [part 1/2]' for text in 'ef'
    } | {compose([REQUIRED, Block('evidence', text * 70000)]).comments[0][0] + ' [part 2/2]' for text in 'ef'}


def test_a_failed_comment_is_reported_and_a_rate_limit_still_waits(tmp_path, monkeypatch):
    from foragent.orch_events import events

    ctx = _ctx(tmp_path)
    body = compose([REQUIRED, Block('evidence', 'e' * 70000)])

    def refuse(*_args):
        raise TrackerError('comment rejected')

    monkeypatch.setattr(ctx.tracker, 'comment', refuse)
    publish_comments(ctx, 9, body)
    assert events(ctx.store, 'pr-full-text-not-retained')

    def limited(*_args):
        raise TrackerError('API rate limit exceeded')

    monkeypatch.setattr(ctx.tracker, 'comment', limited)
    with pytest.raises(TrackerError):
        publish_comments(ctx, 9, body)


@pytest.mark.parametrize('content', [None, 'not json', '[]', '{"summary": 3}', '{"summary": "  "}', '{"other": "x"}'])
def test_no_usable_summary_means_the_issue_title_and_the_log_says_so(tmp_path, content):
    from foragent.run_publish_pr import _summary

    (tmp_path / '.foragent').mkdir()
    if content is not None:
        (tmp_path / '.foragent/PR.json').write_text(content, encoding='utf-8')
    ctx = SimpleNamespace(tracker=SimpleNamespace(get_issue=lambda n: SimpleNamespace(title='The issue title')))
    attempt = SimpleNamespace(n=7, ctx=ctx, worktree=tmp_path, log=tmp_path / 'run.log')
    assert _summary(attempt) == 'The issue title'
    assert 'issue title' in attempt.log.read_text(encoding='utf-8')


def test_a_written_summary_is_used_as_it_is(tmp_path):
    from foragent.run_publish_pr import _summary

    (tmp_path / '.foragent').mkdir()
    (tmp_path / '.foragent/PR.json').write_text(json.dumps({'summary': 'One.\r\nTwo.'}), encoding='utf-8')
    attempt = SimpleNamespace(n=7, ctx=None, worktree=tmp_path, log=tmp_path / 'run.log')
    assert _summary(attempt) == 'One.\r\nTwo.' and not attempt.log.exists()


def _published(c, summary='Summary value.'):
    from foragent.runner import run_issue

    c.executor.steps[0]['.foragent/PR.json'] = json.dumps({'summary': summary})
    c.executor.steps[0]['.foragent/TRY.md'] = '## What changed\r\nA card, not a summary.\r\n'
    c.ctx.config = replace(c.ctx.config, review='careful')
    assert run_issue(1, c.ctx) == 'pr-open'
    c.now[0] += timedelta(seconds=1)
    return c.tracker.created_prs[0]


def test_the_record_holds_rendering_inputs_and_no_finished_body(run_case):  # noqa: F811
    c = run_case
    pr = _published(c)
    values = load_values(1, c.ctx)
    assert values['summary'] == 'Summary value.'
    assert values['try_text'] == '## What changed\r\nA card, not a summary.\r\n'
    assert {'command', 'exit_code', 'timed_out'} == set(values['checks'][0])
    assert 'Closes #1' not in json.dumps(values) and pr.body not in json.dumps(values)
    assert 'Summary value.' in pr.body and 'A card, not a summary.' in pr.body


def test_refresh_without_a_record_stops_before_any_write(run_case, monkeypatch):  # noqa: F811
    from foragent.runner import review_only

    c = run_case
    pr = _published(c)
    (c.store.root / 'pr-content/1.json').unlink()
    enable_reviewer(c, ['VERDICT: PASS'] * 5)
    c.ctx.config = replace(c.ctx.config, review='fast', review_scope='pr-body')
    calls = []
    monkeypatch.setattr(c.tracker, 'update_pr', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(c.tracker, 'merge_pr', lambda *a, **k: calls.append(a))
    assert review_only(1, c.ctx) == 'pr-body-values-missing'
    assert calls == [] and c.tracker.created_prs[0].body == pr.body
    assert any('no pr-content record' in comment.body for comment in c.tracker.list_comments(1))


def test_a_review_refresh_posts_its_comments_before_the_review_reads_the_pr(run_case, monkeypatch):  # noqa: F811
    from foragent import run_review
    from foragent.runner import review_only

    c = run_case
    c.executor.steps[0]['.foragent/SEARCHED.md'] = 'e' * 70000
    pr = _published(c)
    # As if the first publication's comments failed: the next refresh must send them.
    c.tracker.comments[pr.number] = []
    seen = []
    review = run_review._review

    def checked(*args, **kwargs):
        seen.append([comment.body.split(' [part')[0] for comment in c.tracker.list_comments(pr.number)
                     if comment.body.startswith('Foragent full ')])
        return review(*args, **kwargs)

    monkeypatch.setattr(run_review, '_review', checked)
    enable_reviewer(c, ['VERDICT: PASS'] * 5)
    c.ctx.config = replace(c.ctx.config, review='fast', review_scope='pr-body')
    assert review_only(1, c.ctx) == 'merged'
    assert seen and any(name.startswith('Foragent full evidence') for name in seen[0])


def test_a_local_re_review_records_its_verdict_for_the_next_composition(run_case):  # noqa: F811
    from foragent.runner import review_only

    c = run_case
    _published(c)
    enable_reviewer(c, ['VERDICT: PASS'])
    c.ctx.config = replace(c.ctx.config, review='fast')
    assert review_only(1, c.ctx) == 'merged'
    assert [review['verdict'] for review in load_values(1, c.ctx)['reviews']][-1] == 'PASS'


@pytest.mark.parametrize('head,expected', [('foragent/issue-7', None), ('foragent/dream-lane', None), ('someone/else', None)])
def test_merge_retitle_has_no_body_without_a_record(tmp_path, head, expected):
    assert merge_retitle_body(head, _ctx(tmp_path)) is expected
    assert read_values(7, _ctx(tmp_path)) is None


def _retitle(ctx, body, *, title='New (#1)', current='Old'):
    from foragent.run_publish import retitle_for_merge

    notes = []
    subject = retitle_for_merge(ctx, 1, title=title, current_title=current, base='main', body=body, note=notes.append)
    return subject, notes


def _with_pr(tmp_path):
    ctx = _ctx(tmp_path)
    pr = ctx.tracker.create_pr(head='foragent/issue-1', base='main', title='Old', body='Old body')
    assert pr == 1
    return ctx


def test_retitle_without_a_record_changes_the_title_and_writes_no_body(tmp_path):
    ctx = _with_pr(tmp_path)
    subject, notes = _retitle(ctx, lambda: None)
    assert subject == {} and 'no composition record' in notes[0]
    pr = ctx.tracker.created_prs[0]
    assert pr.title == 'New (#1)' and pr.body == 'Old body'


def test_a_failed_title_change_without_a_record_merges_with_the_subject(tmp_path, monkeypatch):
    ctx = _with_pr(tmp_path)

    def refuse(*_args, **_kwargs):
        raise TrackerError('title rejected')

    monkeypatch.setattr(ctx.tracker, 'retitle_pr', refuse)
    subject, notes = _retitle(ctx, lambda: None)
    assert subject == {'subject': 'New (#1)'} and 'title rejected' in notes[-1]
    assert ctx.tracker.created_prs[0].body == 'Old body'


def test_no_record_merge_sends_the_issue_title_as_subject_and_no_body_at_gh():
    """At the gh boundary: the title edit carries no body, and the squash subject is that title."""
    from foragent.tracker import GhTracker

    commands = []

    def command(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[:2] == ['pr', 'view']:
            return json.dumps({'title': 'New (#1)'})
        return ''

    github = GhTracker('o/r', command)
    github.retitle_pr(1, title='New (#1)')
    edit, kwargs = commands[0]
    assert edit[:2] == ['pr', 'edit'] and edit[edit.index('--title') + 1] == 'New (#1)'
    assert '--body-file' not in edit and '--body' not in edit and kwargs.get('input') is None
    github.merge_pr(1, expected_head_sha='a' * 40)
    merge = commands[-1][0]
    assert merge[:2] == ['pr', 'merge'] and merge[merge.index('--subject') + 1] == 'New (#1)'


def test_retitle_with_a_record_sends_the_composition_and_its_comments(tmp_path):
    ctx = _with_pr(tmp_path)
    body = compose([REQUIRED, Block('evidence', 'e' * 70000)])
    assert _retitle(ctx, lambda: body) == ({}, [])
    pr = ctx.tracker.created_prs[0]
    assert pr.title == 'New (#1)' and pr.body == body
    assert ctx.tracker.list_comments(1)[0].body.startswith(body.comments[0][0])


def test_retitle_with_the_right_title_composes_nothing(tmp_path):
    ctx = _with_pr(tmp_path)

    def composed():
        raise AssertionError('No body is needed when the title is already right')

    assert _retitle(ctx, composed, title='Old') == ({}, [])


def test_merge_retitle_composes_from_the_record_and_never_from_the_old_body(run_case):  # noqa: F811
    c = run_case
    pr = _published(c)
    first = merge_retitle_body(pr.head, c.ctx)
    c.tracker.update_pr(pr.number, base=pr.base, title=pr.title, body='A person edited this.')
    assert merge_retitle_body(pr.head, c.ctx) == first and 'A person edited' not in first
    assert 'Closes #1' in first and 'Summary value.' in first


def test_merge_retitle_writes_nothing_when_its_record_cannot_be_composed(run_case):  # noqa: F811
    c = run_case
    pr = _published(c)
    save_values(1, c.ctx, {**load_values(1, c.ctx), 'summary': 's' * 70000})
    assert merge_retitle_body(pr.head, c.ctx) is None


def test_merge_retitle_body_of_a_batch_comes_from_the_tracker(run_case):  # noqa: F811
    """A retired foragent/batch-N head gets no aggregate body; a parented run merges on the base."""
    from foragent.runner import run_issue

    c = run_case
    c.tracker.add_issue(99, "The whole feature")
    c.tracker.set_parent(1, 99)
    c.tracker.set_pr_files(100, ["feature.txt"])
    c.tracker.set_pr_checks(100, "pass")
    assert run_issue(1, c.ctx) == 'merged'
    assert [pr.base for pr in c.tracker.created_prs] == [c.ctx.config.base_branch]
    assert merge_retitle_body('foragent/batch-99', c.ctx) is None


def test_an_update_after_a_review_also_posts_its_comments(tmp_path):
    from foragent.run_publish_pr import _update_open_pr

    ctx = _with_pr(tmp_path)
    body = compose([REQUIRED, Block('evidence', 'e' * 70000)])
    attempt = SimpleNamespace(ctx=ctx, target_branch='main', pr_title=None, log=tmp_path / 'log')
    _update_open_pr(attempt, 1, 'New (#1)', body)
    assert ctx.tracker.created_prs[0].body == body
    assert ctx.tracker.list_comments(1)[0].body.startswith(body.comments[0][0])


def test_a_held_harness_publication_reports_a_body_it_cannot_compose(tmp_path, monkeypatch):
    from foragent import harness_guard, run_publish
    from foragent.config import Config

    ctx = _ctx(tmp_path)
    ctx.config, ctx.repo = Config(), 'o/r'
    ctx.tracker.add_issue(1, title='Feature')
    ctx.store.start_run(1, branch='foragent/issue-1', worktree=tmp_path, lane='main')

    def too_long(*_args):
        raise ExecutorStopped('pr-body-too-long', 'Required PR content is too long')

    monkeypatch.setattr(run_publish, 'commit_exclusions', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_push_branch', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_render_pr_body', too_long)
    attempt = SimpleNamespace(n=1, ctx=ctx, branch='foragent/issue-1', target_branch='main', failure='held', pr_title=None)
    assert harness_guard.publish_hold(attempt) == 'held'
    assert not ctx.tracker.created_prs
    assert any('Required PR content is too long' in comment.body for comment in ctx.tracker.list_comments(1))


def test_a_held_harness_publication_posts_the_comments_its_notes_name(tmp_path, monkeypatch):
    from foragent import harness_guard, run_publish
    from foragent.config import Config

    ctx = _ctx(tmp_path)
    ctx.config, ctx.repo = Config(), 'o/r'
    ctx.tracker.add_issue(1, title='Feature')
    ctx.store.start_run(1, branch='foragent/issue-1', worktree=tmp_path, lane='main')
    body = compose([REQUIRED, Block('evidence', 'e' * 70000)])
    monkeypatch.setattr(run_publish, 'commit_exclusions', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_push_branch', lambda *_a: None)
    monkeypatch.setattr(run_publish, '_render_pr_body', lambda *_a: body)
    attempt = SimpleNamespace(n=1, ctx=ctx, branch='foragent/issue-1', target_branch='main', failure='held', pr_title=None)
    assert harness_guard.publish_hold(attempt) == 'held'
    pr = ctx.tracker.created_prs[0]
    assert pr.body == body and ctx.tracker.list_comments(pr.number)[0].body.startswith(body.comments[0][0])


def test_an_early_native_publication_carries_the_try_text_as_it_is(run_case, monkeypatch):  # noqa: F811
    from foragent.runner import run_issue

    c = run_case
    card = '## What changed\r\nAn early card.\r\n'
    c.executor.steps[0].update({'.foragent/PR.json': json.dumps({'summary': 'Summary value.'}), '.foragent/TRY.md': card})
    enable_reviewer(c, ['VERDICT: PASS'] * 5)
    c.ctx.config = replace(c.ctx.config, review_scope='pr-body')
    sent = []
    create = c.tracker.create_pr
    monkeypatch.setattr(c.tracker, 'create_pr', lambda **kwargs: sent.append(kwargs['body']) or create(**kwargs))
    assert run_issue(1, c.ctx) == 'merged'
    assert card in sent[0] and 'Summary value.' in sent[0]


def test_fixed_format_writers_stay_structured_values():
    composed = compose(['Temporary acceptance comparison on abc.'])
    assert composed == 'Temporary acceptance comparison on abc.' and composed.comments == []


def test_the_prompt_asks_for_the_summary_as_a_value():
    import inspect

    from foragent.run_prompts import build_prompt

    assert '.foragent/PR.json' in inspect.getsource(build_prompt)

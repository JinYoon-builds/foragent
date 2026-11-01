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

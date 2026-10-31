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

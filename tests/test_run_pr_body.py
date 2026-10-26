from foragent.issue import parse_body
from foragent.run_checks import CheckResult
from foragent.run_pr_body import _pr_body


def test_check_commands_are_escaped_and_failed_checks_are_visible():
    body = _pr_body(7, parse_body(''), [CheckResult('a | b\n<x> & `y`', 1, False, '')], 2, [],
                    outside_lane=['other.py'], skills_line='Skills used: none', try_text='Try this')
    assert '| <code>a &#124; b<br>&lt;x> &amp; &#96;y&#96;</code> | fail |' in body
    assert 'Closes #7' in body and '- `other.py`' in body
    assert body.endswith('## Try it\n\nTry this')

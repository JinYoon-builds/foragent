"""Per-file caps require a common path across every participating run."""

import pytest

from foragent.path_overlap import _file_patterns_overlap


@pytest.mark.parametrize('patterns,expected', [
    (('src/**', 'src/a.py', 'src/b.py'), False),
    (('src/**', 'src/[ab].py', 'src/a.py'), True),
    (('src/[ab].py', 'src/[bc].py', 'src/[ac].py'), False),
    (('src/**/a*.py', 'src/pkg/**', 'src/pkg/deep/ab.py'), True),
    (('src/**/a*.py', 'src/pkg/**', 'src/pkg/deep/b.py'), False),
])
def test_multiway_intersection(patterns, expected):
    assert _file_patterns_overlap(*patterns) is expected

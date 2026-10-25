import pytest

from foragent.order import PlanError
from foragent.order_files import validate_paths


@pytest.mark.parametrize('value', ['src/**', 'new file.py', 'src/[ab].py'])
def test_exclusive_paths_accept_repository_paths_and_globs(tmp_path, value):
    validate_paths({'exclusive': [value]}, tmp_path, 'issues[0]')


@pytest.mark.parametrize('value', [None, 'src/a.py', [None], [''], ['a\nb'], ['../outside'], ['/outside']])
def test_exclusive_paths_reject_invalid_lists_and_escaping_paths(tmp_path, value):
    with pytest.raises(PlanError, match='exclusive'):
        validate_paths({'exclusive': value}, tmp_path, 'edits[0]')

import pytest

from foragent.issue import parse_body
from foragent.issue_metadata import exclusive_header


@pytest.mark.parametrize('path', ['src/[ab].py', 'space and # hash.py', 'yes', 'a: b'])
def test_exclusive_metadata_round_trips_yaml_path_strings(path):
    body = '\n'.join(['<!-- foragent:v1 -->', *exclusive_header([path]), '## Goal', 'Goal',
                      '## Files', '- other.py'])
    parsed = parse_body(body)
    assert parsed.exclusive == [path] and parsed.files == ['other.py']


def test_body_sections_do_not_supply_exclusive_metadata():
    assert parse_body('<!-- foragent:v1 -->\n## Goal\nexclusive:\n  - other.py').exclusive == []

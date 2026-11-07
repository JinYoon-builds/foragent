"""Guard for lockonly-help: the word matcher separates each claim from its opposite.

These cases already pass on main, so this file stays outside Done when.
"""

import pytest

from lockonly_help_words import COVERED, LOCK_ONLY, _states


@pytest.mark.parametrize("text, words, expected", [
    ("An edit that changes only an issue's existing locked acceptance files publishes without "
     "test preparation. The publication recheck proves it.", LOCK_ONLY, True),
    ("An edit that changes only an issue's existing locked acceptance files needs test "
     "preparation. The publication recheck proves it.", LOCK_ONLY, False),
    ("An edit that changes an issue's existing locked acceptance files publishes without "
     "test preparation. The publication recheck proves it.", LOCK_ONLY, False),
    ("An edit that changes only an issue's existing locked acceptance files publishes without "
     "test preparation.", LOCK_ONLY, False),
    ("An edit that changes only an issue's existing locked acceptance files needs no test "
     "preparation; the publication recheck proves it.", LOCK_ONLY, True),
    ("When an edit is prepared, statements the issue's existing locked tests already cover "
     "count as covered.", COVERED, True),
    ("When an edit is prepared, every statement needs new tests even if existing locked tests "
     "cover it.", COVERED, False),
])
def test_the_word_check_separates_the_claim_from_its_opposite(text, words, expected):
    assert _states(text, words) is expected

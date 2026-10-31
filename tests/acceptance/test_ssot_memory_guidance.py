"""Locked public skill contract: producers append lessons, the dream curates."""
from pathlib import Path

import pytest


def repo_root():
    return next(parent for parent in Path(__file__).resolve().parents if (parent / '.foragent.yml').is_file())


@pytest.mark.parametrize('skill', ['fa', 'fa-order'])
def test_lightest_fix_appends_pending_lessons_in_both_public_guides(skill):
    """Reject changing worker instructions while the planning entry guides still grant another writer."""
    text = (repo_root() / 'skills' / skill / 'SKILL.md').read_text()
    start = text.lower().index('lightest fix')
    # Only the first, cheapest option is under test; skill wording can otherwise evolve.
    option = text[start:].split('2.', 1)[0]
    assert 'MEMORY.new.md' in option, f'{skill} still directs the lightest fix to curated memory'
    assert 'append' in option.lower(), f'{skill} must append pending lessons'
    assert 'MEMORY.md` of the lane\n' not in option, 'The conflicting direct-edit procedure must be removed'

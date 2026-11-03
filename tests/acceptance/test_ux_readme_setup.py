"""The published install instructions answer the first-user questions."""

from pathlib import Path

import pytest


README = Path(__file__).resolve().parents[2] / 'README.md'


@pytest.mark.parametrize('language', ['en', 'ko'])
def test_readme_explains_uv_and_prerequisites_before_install(language):
    readme = README.read_text(encoding='utf-8')
    english, korean = readme.split('## 한국어', 1)
    section = english if language == 'en' else korean
    recipe = 'uv tool install git+https://github.com/JinYoon-builds/foragent'
    before = section[:section.index(recipe)]
    assert 'curl -LsSf https://astral.sh/uv/install.sh | sh' in before or (
        'https://docs.astral.sh/uv/getting-started/installation/' in before
    ), 'Explain how to get uv before the first command that needs it'
    for term in ('Git', 'GitHub', 'gh', 'Claude Code', 'origin'):
        assert term in before, f'Missing prerequisite before install: {term}'
    assert 'gh auth login' in before
    assert 'fa init' in section and '/fa' in section


def test_readme_removes_placeholder_and_stale_count():
    readme = README.read_text(encoding='utf-8')
    assert 'GIF placeholder' not in readme
    assert '208 merged PRs in 10 days' not in readme
    assert 'counted from `git log` on 2026-10-29' not in readme

"""#1652: repository defaults at executor-visible session inputs.

Reuse #1619's offline transports and public session starts. Only fixture file
serialization changes: actual repository SKILL.md and LICENSE bytes replace
synthetic guidance. No selection, license check or prompt builder is replaced.
"""
from pathlib import Path
import re

import pytest
import yaml

import test_default_skill_sessions as sessions
from test_skills_autosync import isolated_home  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
TABLE = {
    'workers': ['prove-it-works', 'fix-root-causes', 'attack-the-premise', 'sequence-verifiable-units'],
    'reviewers': ['prove-it-works', 'attack-the-premise', 'test-behavior-not-implementation'],
    'orchestrator': ['prove-it-works', 'attack-the-premise', 'never-block-on-the-human',
                     'encode-lessons-in-structure'],
    'architect': ['attack-the-premise', 'redesign-from-first-principles', 'exhaust-the-design-space',
                  'separate-before-serializing-shared-state', 'subtract-before-you-add',
                  'foundational-thinking'],
    'docs': ['minimize-reader-load'],
}  # D117 (Jin 2026-11-04): two-layer default skills, layer A.
TABLE = {role: [f'principle-{name}' for name in names] for role, names in TABLE.items()}
TABLE['reviewers'].append('review-procedure')  # #1651: the review procedure is a reviewer default skill.
ALL = {name for names in TABLE.values() for name in names}
DOCUMENTS = ('docx', 'pdf', 'pptx', 'xlsx')


@pytest.fixture
def repository_payload(monkeypatch):
    original = sessions.skill_files
    payload = {f'skills/{name}/{file}': (ROOT / 'skills' / name / file).read_text()
               for name in ALL for file in ('SKILL.md', 'LICENSE')}
    # These known source-available tools are present but not assigned to any role.
    for name in DOCUMENTS:
        payload[f'skills/{name}/SKILL.md'] = (
            f'---\nname: {name}\ndescription: Document fixture\n---\nDOCUMENT::{name}\n')
        payload[f'skills/{name}/LICENSE'] = 'SPDX-License-Identifier: LicenseRef-Source-Available\n'

    def files(names=None, license='MIT'):
        return {**original(names, license), **payload}

    monkeypatch.setattr(sessions, 'skill_files', files)
    return yaml.safe_load((ROOT / '.foragent.yml').read_text()).get('default_skills')


def test_repository_configuration_names_only_the_approved_defaults():
    written = yaml.safe_load((ROOT / '.foragent.yml').read_text()).get('default_skills')
    assert written == TABLE


@pytest.mark.parametrize('consumer,executor', [
    *[(role, executor) for role in ('workers', 'no-lane', 'reviewers', 'architect', 'docs')
      for executor in ('codex', 'claude')],
    ('orchestrator', 'claude'),
])
def test_repository_table_reaches_exactly_its_role(
    git_history, tmp_path, monkeypatch, repository_payload, consumer, executor,
):
    role = 'workers' if consumer == 'no-lane' else consumer
    inputs = sessions.run_consumer(consumer, executor, git_history, tmp_path, monkeypatch,
                                  repository_payload, available=(*ALL, *DOCUMENTS))
    assert inputs, 'The public session start must actually invoke an executor'
    for value in inputs:
        # A declaration without a readable skill path is not delivered guidance.
        reached = set(re.findall(r'(?:\.claude/)?skills/([^/\s)]+)/SKILL\.md', value))
        reached -= {'fa-orchestrator', 'fa-architect', 'fa-docs'}  # Existing role procedures.
        assert reached == set(TABLE[role]), (role, reached)
        for name in TABLE[role]:
            source = (ROOT / 'skills' / name / 'SKILL.md').read_text()
            assert source in value, (role, name, 'The linked guidance must be readable')
        # Document tools must not enter the prompt, agent skills, or referenced files.
        assert not any(f'skills/{name}/' in value or f'DOCUMENT::{name}' in value
                       or re.search(rf'(?m)^- {name}(?::|\s|$)', value)
                       for name in DOCUMENTS)

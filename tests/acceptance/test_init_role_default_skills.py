"""D117 (2026-11-04): a new project's role default skills, each shown with its source.

`fa init` on a new project writes the approved per-role table and installs those skills.
Every skill table a person or a session sees names each skill's source: pstack (MIT),
Anthropic official (Apache-2.0) or Foragent-made. An existing project is not rewritten.
Offline transports come from the existing init and session fixtures; nothing in the
selection, license check or prompt builder is replaced.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

import test_default_skill_sessions as sessions
from test_init import setup  # noqa: F401
from test_skills_autosync import isolated_home, write_global_config  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
P = 'principle-'
TABLE = {
    'workers': [f'{P}prove-it-works', f'{P}fix-root-causes', f'{P}attack-the-premise',
                f'{P}sequence-verifiable-units'],
    'reviewers': [f'{P}prove-it-works', 'review-procedure', f'{P}attack-the-premise',
                  f'{P}test-behavior-not-implementation'],
    'orchestrator': [f'{P}prove-it-works', f'{P}attack-the-premise', f'{P}never-block-on-the-human',
                     f'{P}encode-lessons-in-structure'],
    'architect': [f'{P}attack-the-premise', f'{P}redesign-from-first-principles',
                  f'{P}exhaust-the-design-space', f'{P}separate-before-serializing-shared-state',
                  f'{P}subtract-before-you-add', f'{P}foundational-thinking'],
    'docs': [f'{P}minimize-reader-load'],
}
ALL = sorted({name for names in TABLE.values() for name in names})
# The source each shown row must name, with its license where D117 gives one.
SOURCE = {name: ('pstack', 'MIT') for name in ALL}
SOURCE['review-procedure'] = ('Anthropic official', 'Apache-2.0')
INIT = ['init', '--approver', 'human', '--no-push', '--no-launchd', '--no-board', '--statusline', 'no']


def run_init(setup, monkeypatch):
    from foragent import cli
    from foragent.git import GitRepo
    monkeypatch.setattr(GitRepo, 'commit_paths', lambda *a, **kw: None)
    monkeypatch.setattr(cli, 'init_repo', lambda root, **kw: setup.init(**kw))
    return cli.main([INIT[0], '--repo-root', str(setup.root), *INIT[1:]])


def written_table(root):
    return (yaml.safe_load((root / '.foragent.yml').read_text()) or {}).get('default_skills')


def test_new_project_init_writes_and_installs_the_approved_role_table(setup, isolated_home, monkeypatch):
    assert run_init(setup, monkeypatch) == 0
    written = written_table(setup.root)
    assert isinstance(written, dict), 'A new project must get a default_skills table'
    for role, names in TABLE.items():
        entries = written.get(role) or []
        assert len(entries) == len(set(entries)), (role, 'each default once')
        assert {entry.rsplit(':', 1)[-1].rsplit('/', 1)[-1] for entry in entries} == set(names), role
    assert not set(written) - set(TABLE), 'Workers keep their table; no other role is invented'
    for name in ALL:
        installed = setup.root / '.claude/skills' / name / 'SKILL.md'
        assert installed.read_bytes() == (ROOT / 'skills' / name / 'SKILL.md').read_bytes(), name


@pytest.mark.parametrize('existing', ['no-table', 'own-table', 'after-first-init'])
def test_existing_project_keeps_its_table(setup, isolated_home, monkeypatch, existing):
    config = setup.root / '.foragent.yml'
    if existing == 'after-first-init':
        assert run_init(setup, monkeypatch) == 0
    else:
        values = {'review': 'fast', 'approvers': ['human'], 'checks': 'local'}
        if existing == 'own-table':
            values['default_skills'] = {'docs': [], 'workers': [f'{P}prove-it-works']}
        config.write_text(yaml.safe_dump(values))
    before = written_table(setup.root)
    assert run_init(setup, monkeypatch) == 0
    assert written_table(setup.root) == before
    if existing == 'no-table':
        assert not any((setup.root / '.claude/skills' / name).exists() for name in ALL)


@pytest.fixture
def repository_payload(monkeypatch):
    original = sessions.skill_files
    payload = {f'skills/{name}/{file}': (ROOT / 'skills' / name / file).read_text()
               for name in ALL for file in ('SKILL.md', 'LICENSE', 'NOTICE')
               if (ROOT / 'skills' / name / file).is_file()}
    monkeypatch.setattr(sessions, 'skill_files', lambda names=None, license='MIT': {
        **original(names, license), **payload})


def shows_source(line, name):
    label, license = SOURCE[name]
    folded = line.casefold()
    return label.casefold() in folded and license.casefold() in folded


@pytest.mark.parametrize('consumer,executor', [
    ('workers', 'codex'), ('reviewers', 'claude'), ('orchestrator', 'claude'),
    ('architect', 'codex'), ('docs', 'claude'),
])
def test_session_default_table_names_each_skills_source(
    git_history, tmp_path, monkeypatch, repository_payload, consumer, executor,
):
    inputs = sessions.run_consumer(consumer, executor, git_history, tmp_path, monkeypatch, TABLE, available=ALL)
    assert inputs, 'The public session start must actually invoke an executor'
    for value in inputs:
        lines = value.splitlines()
        # The skill's own files mention their upstream; only the table rows a session is shown count.
        shipped = {line for name in ALL for file in ('SKILL.md', 'LICENSE', 'NOTICE')
                   if (ROOT / 'skills' / name / file).is_file()
                   for line in (ROOT / 'skills' / name / file).read_text().splitlines()}
        for name in TABLE[consumer]:
            rows = [line for line in lines if line not in shipped
                    and re.search(rf'(?:\.claude/)?skills/{re.escape(name)}/SKILL\.md', line)]
            assert rows, (consumer, name, 'The default must be listed with its path')
            assert any(shows_source(line, name) for line in rows), (consumer, name, SOURCE[name], rows)
        for name in set(ALL) - set(TABLE[consumer]):
            assert not any(f'skills/{name}/SKILL.md' in line for line in lines), (consumer, name)


def test_skill_catalog_names_the_source_of_each_row(isolated_home, tmp_path, capsys):
    from foragent import cli
    libraries = {'anthropic-fixture': 'https://github.com/anthropics/skills',
                 'independent-fixture': 'https://example.org/independent/skills'}
    entries = []
    for library, repo in libraries.items():
        path = tmp_path / library
        skill = path / 'skills' / f'{library}-guide'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {library}-guide\ndescription: Fixture guide\n---\nGuide.\n')
        (path / 'LICENSE').write_text((ROOT / 'LICENSE-APACHE').read_text())
        entries.append({'name': library, 'repo': repo, 'path': str(path)})
    write_global_config({'skill_libraries': entries})
    assert cli.main(['skills', 'catalog']) == 0
    rows = capsys.readouterr().out.splitlines()
    assert cli.main(['skills', 'catalog', '--json']) == 0
    records = json.loads(capsys.readouterr().out)

    def shown(name):
        return ([row for row in rows if f'\t{name}\t' in row],
                [json.dumps(record) for record in records if record.get('name') == name])

    for text, record in (shown('anthropic-fixture-guide'),):
        assert text and record
        assert all('anthropic official' in item.casefold() for item in (*text, *record))
    text, record = shown('independent-fixture-guide')
    assert text and record and not any('anthropic official' in item.casefold() for item in (*text, *record))
    text, record = shown(f'{P}prove-it-works')
    assert text and record
    assert all('pstack' in item and 'MIT' in item and 'anthropic official' not in item.casefold()
               for item in (*text, *record))

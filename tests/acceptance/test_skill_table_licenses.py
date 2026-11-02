"""#1652: LICENSE eligibility through fa init, with no license-check internals."""
from pathlib import Path

import pytest

from test_init import setup  # noqa: F401
from test_skills_autosync import isolated_home, write_global_config  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
PRINCIPLES = (
    'prove-it-works', 'fix-root-causes', 'attack-the-premise',
    'sequence-verifiable-units', 'redesign-from-first-principles',
)
SHIPPED = (ROOT / 'skills/principle-prove-it-works/LICENSE').read_text()
MIT = SHIPPED.split('\n', 4)[4]
NOTICE = (
    'SPDX-License-Identifier: MIT\n'
    'nebula by Quinn Example, MIT License, Copyright (c) 2026 Quinn Example\n'
    'Upstream: https://example.org/independent/nebula\n'
)


def initialize(setup, monkeypatch, license_text):
    """Supply offline Git/GitHub transports; run the real public init CLI."""
    from foragent import cli
    from foragent.git import GitRepo

    library = setup.root.parent / 'independent-library'
    skill = library / 'skills/independent-guidance'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: independent-guidance\ndescription: Independent example\n---\nGuidance.\n')
    (skill / 'LICENSE').write_text(license_text)
    write_global_config({
        'skill_libraries': [{'name': 'independent', 'repo': 'https://example.org/independent',
                             'path': str(library)}],
        'default_skills': {'docs': ['independent-guidance']},
    })
    monkeypatch.setattr(GitRepo, 'commit_paths', lambda *a, **kw: None)
    monkeypatch.setattr(cli, 'init_repo', lambda root, **kw: setup.init(**kw))
    code = cli.main(['init', '--repo-root', str(setup.root), '--approver', 'human',
                     '--no-push', '--no-launchd', '--no-board', '--statusline', 'no'])
    return code, skill, setup.root / '.claude/skills/independent-guidance'


@pytest.mark.parametrize('principle', PRINCIPLES)
def test_shipped_license_is_accepted_unchanged(setup, isolated_home, monkeypatch, principle):
    license_text = (ROOT / f'skills/principle-{principle}/LICENSE').read_text()
    code, source, installed = initialize(setup, monkeypatch, license_text)
    assert code == 0
    assert (installed / 'LICENSE').read_bytes() == (source / 'LICENSE').read_bytes()
    assert (installed / 'SKILL.md').read_bytes() == (source / 'SKILL.md').read_bytes()


@pytest.mark.parametrize('license_text', [
    NOTICE + '\n' + MIT,
    MIT + '\n' + NOTICE,
    'Source: https://example.org/source\nhttps://example.org/credits\n' + NOTICE + MIT,
    NOTICE + MIT + '\nAttribution: https://example.org/credits\n',
    NOTICE + MIT.replace('Copyright (c) 2026 Lauren Tan',
                         'Copyright 2026 Example, Redistribution is prohibited.'),
    'SPDX-License-Identifier: Apache-2.0\nSource: https://example.org/apache\n'
    + (ROOT / 'LICENSE-APACHE').read_text(),
], ids=['external-prefix', 'external-suffix', 'source-and-url', 'attribution-url',
        'copyright-name-not-judged', 'another-permissive-license'])
def test_permissive_text_with_notices_is_accepted(setup, isolated_home, monkeypatch, license_text):
    code, source, installed = initialize(setup, monkeypatch, license_text)
    assert code == 0
    assert (installed / 'LICENSE').read_bytes() == (source / 'LICENSE').read_bytes()


@pytest.mark.parametrize('license_text', [
    'Redistribution is prohibited.\n' + SHIPPED,
    SHIPPED + '\nRedistribution is prohibited.\n',
    NOTICE + 'Redistribution is prohibited.\n' + MIT,
    NOTICE + MIT + '\nYou must obtain written permission before commercial use.\n',
    SHIPPED.replace('SPDX-License-Identifier: MIT', 'SPDX-License-Identifier: GPL-3.0-only', 1),
    SHIPPED.replace('SPDX-License-Identifier: MIT', 'SPDX-License-Identifier: CC-BY-NC-4.0', 1),
    SHIPPED.replace('SPDX-License-Identifier: MIT', 'SPDX-License-Identifier: LicenseRef-Source-Available', 1),
    SHIPPED.replace('free of charge, ', '', 1),
    SHIPPED.split('THE SOFTWARE IS PROVIDED')[0],
    NOTICE + MIT + '\nSource: https://example.org/source Redistribution is prohibited.\n',
    NOTICE + MIT + '\nAttribution: Redistribution is prohibited.\n',
    NOTICE,  # SPDX plus arbitrary notices alone cannot substitute for full terms.
], ids=['ban-before', 'ban-after', 'ban-between', 'extra-condition', 'copyleft-spdx',
        'noncommercial-spdx', 'source-available-spdx', 'changed-terms', 'truncated-terms',
        'source-label-with-condition', 'attribution-label-with-condition', 'no-full-license'])
def test_extra_terms_or_nonpermissive_declarations_are_rejected(
    setup, isolated_home, monkeypatch, capsys, license_text,
):
    code, _, installed = initialize(setup, monkeypatch, license_text)
    assert code != 0
    assert not installed.exists()
    assert 'license' in capsys.readouterr().err.lower()

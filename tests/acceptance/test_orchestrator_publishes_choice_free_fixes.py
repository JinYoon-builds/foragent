"""Locked: D71 judgment stays with the agent, and choice-free fixes can finish.

Text assertions check public guidance, never classify a work request at runtime.
The CLI controls also establish that clean preread remains mandatory and that a
Foragent role restriction must not be invented where none currently exists.
"""
from pathlib import Path
import re

import pytest

from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread_case, preread  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def repository_root():
    return next(parent for parent in Path(__file__).resolve().parents if (parent / '.foragent.yml').is_file())


def assert_choice_free_boundary(text, source):
    """Check the stated policy without choosing implementation or classifying issue text."""
    text = ' '.join(text.casefold().replace('`', '').split())
    assert 'choice-free' in text and 'd71' in text, f'{source} lacks the scoped D71 choice-free publication boundary'
    autonomy = re.search(
        r'(?:choice-free.{0,180}orchestrator.{0,100}publish(?:es)?.{0,80}(?<!-)\bapprove(?:s)?\b'
        r'|orchestrator.{0,100}publish(?:es)?.{0,80}(?<!-)\bapprove(?:s)?\b.{0,180}choice-free)', text,
    )
    assert autonomy and ('itself' in text or 'on its own' in text), (
        f'{source} must explicitly let the orchestrator publish and approve choice-free fixes itself'
    )
    assert 'fa order publish' in text and '--approve' in text and 'clean preread' in text
    assert 'planning session' in text and 'person' in text and 'question' in text
    assert ('behaviour' in text or 'behavior' in text) and all(
        choice in text for choice in ('scope', 'screen wording', 'approval boundary')
    ), f'{source} must send the actual D71 choices and real preread questions through planning to the person'


@pytest.mark.parametrize('skill', ['fa-orchestrator', 'fa-order'])
def test_guides_authorize_choice_free_orchestrator_publication_with_clean_preread(
    preread_case, monkeypatch, capsys, skill,
):
    """Reject fixing only one guide, keeping a planning-only blanket rule, bypassing
    preread for the orchestrator, or requiring a human decision for every bug fix.
    """
    case = preread_case
    monkeypatch.setenv('FORAGENT_ROLE', 'orchestrator')
    capsys.readouterr()
    before = len(case.worker.calls)
    code = cli_exit(case.main, ['order', 'publish', str(case.path), '--repo-root', str(case.root), '--approve'])
    output = capsys.readouterr()
    assert code != 0 and 'preread' in (output.out + output.err).lower(), 'Missing preread must still refuse orchestrator approval'
    assert case.tracker.issues == {} and case.events == [] and len(case.worker.calls) == before
    preread(case, capsys, 'NONE', 'NONE')
    capsys.readouterr()
    before = len(case.worker.calls)
    code = cli_exit(case.main, ['order', 'publish', str(case.path), '--repo-root', str(case.root), '--approve'])
    output = capsys.readouterr()
    assert code == 0, f'Foragent must allow orchestrator publication after clean preread: {output.out}{output.err}'
    assert len(case.worker.calls) == before
    children = [issue for issue in case.tracker.list_issues() if issue.title in
                {item['title'] for item in case.plan['issues']}]
    assert len(children) == 2 and all('approved' in issue.labels for issue in children)

    # Control: a real QUESTION still refuses approval without changing the issues.
    question = 'Which receipt locale should the person see?'
    preread(case, capsys, 'QUESTION: ' + question, 'NONE')
    issues_before = dict(case.tracker.issues)
    events_before = list(case.events)
    capsys.readouterr()
    before = len(case.worker.calls)
    code = cli_exit(case.main, ['order', 'publish', str(case.path), '--repo-root', str(case.root), '--approve'])
    output = capsys.readouterr()
    assert code != 0 and question in output.out + output.err, 'The orchestrator must not bypass a real preread QUESTION'
    assert case.tracker.issues == issues_before and case.events == events_before
    assert len(case.worker.calls) == before and case.gh == []

    text = (repository_root() / 'skills' / skill / 'SKILL.md').read_text()
    assert_choice_free_boundary(text, skill)

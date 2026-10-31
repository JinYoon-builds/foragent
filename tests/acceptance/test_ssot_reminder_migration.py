"""Locked: standing role instructions preserve lessons, not private work ledgers.

The live inventory migration is agent-led and has an evidence checklist in the
order. This offline check cannot establish current remote dispositions.
"""
from pathlib import Path

from foragent.orch_role_prompt import build_prompt
from test_orch_architect import architect_case, call_architect, finish_session
from test_skills_autosync import isolated_home  # noqa: F401


def test_role_startup_sends_work_facts_to_issues_and_keeps_lessons_in_memory(tmp_path, monkeypatch, capsys):
    """Reject deleting one reminder file while newly started roles recreate the same second owner."""
    root = next(p for p in Path(__file__).resolve().parents if (p / '.foragent.yml').is_file())
    case = architect_case(tmp_path, monkeypatch)
    child = call_architect(case, capsys, why='Continue from issue-owned conditions.')
    finish_session(case, child)
    prompts = {'architect': case.turns[0]['prompt'],
               'docs': build_prompt('Continue from issue-owned conditions.', root, 'codex', role='docs')}
    for role, prompt in prompts.items():
        text = prompt.lower()
        assert 'github issue' in text and 'lesson' in text, f'{role} startup still records work proposals as memory'
        assert 'record each proposal there as one' not in text
        assert 'record each proposal in' not in text
    skill = (root / 'skills/fa-orchestrator/SKILL.md').read_text().lower()
    for phrase in ('waiting reason', 'next condition', 'responsible role'):
        assert phrase in skill, f'Orchestrator lacks the issue-owned continuation context: {phrase}'
    assert not (root / '.claude/agent-memory/fa-orchestrator/pending-after-merge.md').exists()

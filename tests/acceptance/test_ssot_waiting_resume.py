"""Locked: explicit condition changes reach their responsible role once."""
from datetime import timedelta
import json

import pytest

from foragent.scheduler import tick
from foragent.state import StateStore
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


@pytest.mark.parametrize('role', ['orchestrator', 'docs'])
def test_closed_dependency_notifies_owner_of_unapproved_wait_once(git_history, tmp_path, monkeypatch, capsys, role):
    """Reject notifying only approved issues, repeated ticks, or silently granting approval."""
    case = make_case(git_history, tmp_path, monkeypatch, language='en', skill_libraries=[], test_command='true')
    case.store.finish_run(1, 'cancelled')
    case.tracker.remove_labels(1, ['running', 'approved'])
    reason = 'Waiting for the catalog review'
    condition = f'Issue #50 is closed; {role} re-reads the decision and continues'
    body = case.tracker.get_issue(1).body + (
        f'\n## Waiting\nReason: {reason}\nNext condition: {condition}\nResponsible role: {role}\n')
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=body)
    case.tracker.add_issue(50, 'Catalog review')
    case.tracker.add_blocked_by(1, 50)
    record_gh(monkeypatch)
    main = bind_cli(case, monkeypatch)
    context = tick_context(case, start_runs=False)
    assert tick(context).skipped is None
    case.clock[0] += timedelta(minutes=6)
    case.tracker.close_issue(50)
    assert tick(context).skipped is None
    capsys.readouterr()
    assert cli_exit(main, ['orch', 'inbox', '--pending', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)
    matches = [row for row in rows if '#1' in json.dumps(row) and reason in json.dumps(row)]
    assert len(matches) == 1, 'A satisfied explicit wait must reach the responsible role exactly once'
    notice = json.dumps(matches[0])
    assert condition in notice and role in notice
    assert 'approved' not in case.tracker.get_issue(1).labels
    assert not case.store.active_runs(), 'A notification is not permission to start unapproved work'
    case.store = context.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(2):
        case.clock[0] += timedelta(minutes=6)
        assert tick(context).skipped is None
    assert cli_exit(main, ['orch', 'inbox', '--pending', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len([row for row in rows if '#1' in json.dumps(row) and reason in json.dumps(row)]) == 1

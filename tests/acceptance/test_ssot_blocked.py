"""Locked: GitHub's current blocked marker owns the reason; local flags are copies."""
from datetime import timedelta
import json

from foragent.scheduler import tick
from test_orch_verbs import blocked_case
from test_orch_review_handoff import tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def test_tick_reconciles_stale_flag_to_current_marker_and_clears_it_after_unblock(
    git_history, tmp_path, monkeypatch, capsys,
):
    """Reject trusting an old local reason or resurrecting it after GitHub unblocks the issue."""
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    case.clock[0] += timedelta(minutes=1)
    current = case.tracker.comment(1, '<!-- foragent:blocked reason=spec-question -->\nChoose the locale')
    assert tick(tick_context(case, start_runs=False)).skipped is None
    flag = case.store.load_flag(1)
    assert flag and flag.reason == 'spec-question', 'The current GitHub marker must replace the stale local reason'
    assert flag.flagged_at == case.tracker.list_comments(1)[-1].created_at
    assert cli_exit(main, ['orch', 'list', '--json']) == 0
    row, = json.loads(capsys.readouterr().out)
    assert row['reason'] == flag.reason and 'Choose the locale' in row['details']
    assert current in [comment.id for comment in case.tracker.list_comments(1)]
    case.tracker.remove_labels(1, ['blocked', 'needs-human'])
    case.clock[0] += timedelta(minutes=6)
    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert case.store.load_flag(1) is None, 'An unblocked issue must not keep a locally authoritative blocker'
    assert cli_exit(main, ['orch', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not case.tracker.get_issue(1).labels & {'blocked', 'needs-human'}

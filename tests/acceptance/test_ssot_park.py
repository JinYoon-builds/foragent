"""Locked: pruning machine history cannot erase shared parked intent."""
from datetime import timedelta
import json

from foragent.scheduler import tick
from foragent.state import StateStore
from test_orch_park import park_case
from test_orch_review_handoff import tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def test_park_survives_thirty_day_history_prune_and_unparks_from_github(
    git_history, tmp_path, monkeypatch, capsys,
):
    """Reject a local park journal as authority, including parking without waiting-person."""
    case, main = park_case(git_history, tmp_path, monkeypatch)
    why = 'Wait for the catalog owner to confirm the locale'
    assert cli_exit(main, ['orch', 'park', '1', '--why', why]) == 0
    issue = case.tracker.get_issue(1)
    evidence = issue.body + '\n' + '\n'.join(c.body for c in case.tracker.list_comments(1))
    assert why in evidence, 'Park reason must be reconstructible from the GitHub issue'
    assert 'approved' not in issue.labels
    case.clock[0] += timedelta(days=31)
    # The public tick owns the normal 30-day pruning path.
    assert tick(tick_context(case, start_runs=False)).skipped is None
    journal = case.store.root / 'orch-events.jsonl'
    events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    assert not any(row.get('kind') == 'parked' for row in events), 'Control: the old local park event must be pruned'
    case.store = case.ctx.store = StateStore(case.store.root, now=lambda: case.clock[0])
    capsys.readouterr()
    assert cli_exit(main, ['orch', 'unpark', '1']) == 0, 'Unpark must recover parked intent from GitHub after pruning'
    assert 'approved' not in case.tracker.get_issue(1).labels
    before = list(case.tracker.list_comments(1))
    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert case.tracker.list_comments(1) == before, 'An unchanged unparked state must not repeat its transition'

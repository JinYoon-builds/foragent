"""Locked: work waits belong to issues, including before approval.

Waiting is a public body section in these fixtures, not a new internal record shape.
"""
from copy import deepcopy
import json

from foragent import order
from test_order import publishing, plan  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

REASON = 'The locale decision is pending'
CONDITION = 'The person records the locale choice on this issue'
ROLE = 'orchestrator'
WAITING = f'## Waiting\nReason: {REASON}\nNext condition: {CONDITION}\nResponsible role: {ROLE}\n'


def test_unapproved_wait_is_discoverable_from_order_context(publishing, plan, monkeypatch, capsys):
    """Reject storing only labels or filtering all unapproved work out of planning context."""
    root, config, tracker, git, _ = publishing
    plan['issues'] = plan['issues'][:1]
    plan['issues'][0]['goal'] = 'Ship the selected locale.\n\n' + WAITING
    result = order.publish(plan, root, config, tracker, repo='owner/repo', git=git)
    child = next(issue.number for issue in tracker.list_issues() if issue.title == 'Login API')
    assert WAITING.strip() in tracker.get_issue(child).body
    assert 'approved' not in tracker.get_issue(child).labels
    main = publish_cli(publishing, monkeypatch)
    capsys.readouterr()
    assert cli_exit(main, ['order', 'context', '--repo-root', str(root), '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    row = next(row for row in data['open_issues'] if row['number'] == child)
    text = json.dumps(row, ensure_ascii=False)
    for value in (REASON, CONDITION, ROLE):
        assert value in text, f'Planning context lost the issue-owned wait: {value}'


def test_order_edit_replaces_wait_and_native_dependencies_together(publishing, plan, monkeypatch, capsys):
    """Reject an edit that changes prose but leaves the previous native blocker or wait active."""
    root, config, tracker, git, _ = publishing
    plan['issues'] = plan['issues'][:1]
    initial = order.publish(plan, root, config, tracker, repo='owner/repo', git=git)
    body = '<!-- foragent:v1 -->\n## Goal\nShip the locale.\n## Out of scope\nOther changes.\n## Done when\n```sh\ntrue\n```\n'
    child = tracker.add_issue(70, 'Locale client', body, labels=['lane:api', 'size:s']).number
    blocker = next(issue.number for issue in tracker.list_issues() if issue.title == 'Login API')
    changed = deepcopy(plan)
    changed['issues'] = changed['issues'][:1]
    changed['edits'] = [{'number': child, 'goal': 'Ship the locale.\n\n' + WAITING,
                         'blocked_by': ['api'], 'out_of_scope': 'Other product changes.',
                         'done_when': ['true']}]
    path = root / 'edit.json'
    path.write_text(json.dumps(changed))
    main = publish_cli(publishing, monkeypatch)
    code = cli_exit(main, ['order', 'publish', str(path), '--repo-root', str(root)])
    output = capsys.readouterr()
    assert code == 0, f'Order edits must carry dependencies and the wait: {output.err}'
    assert [dep.number for dep in tracker.blocked_by(child)] == [blocker]
    assert REASON in tracker.get_issue(child).body
    changed['edits'][0].update(goal='Ship the chosen locale.\n\n## Waiting\nNone.\n', blocked_by=[])
    path.write_text(json.dumps(changed))
    assert cli_exit(main, ['order', 'publish', str(path), '--repo-root', str(root)]) == 0
    assert tracker.blocked_by(child) == []
    assert REASON not in tracker.get_issue(child).body


def test_wait_changes_comment_once_and_unchanged_publication_stays_quiet(publishing, plan):
    """Reject a silent waiting transition, duplicated comments on republish, or two lines per change."""
    from foragent import order
    from test_ssot_waiting_order import WAITING, REASON
    root, config, tracker, git, _ = publishing
    plan['issues'] = plan['issues'][:1]
    plan['issues'][0]['goal'] += '\n\n' + WAITING
    order.publish(plan, root, config, tracker, repo='owner/repo', git=git)
    child = next(issue.number for issue in tracker.list_issues() if issue.title == 'Login API')
    comments = tracker.list_comments(child)
    waiting = [comment for comment in comments if REASON in comment.body]
    assert len(waiting) == 1 and len(waiting[0].body.splitlines()) == 1, 'A waiting transition owes one comment line'
    order.publish(plan, root, config, tracker, repo='owner/repo', git=git)
    assert tracker.list_comments(child) == comments, 'Unchanged waiting state must not comment again'
    plan['issues'][0]['goal'] = plan['issues'][0]['goal'].replace(REASON, 'Waiting for the catalog owner')
    order.publish(plan, root, config, tracker, repo='owner/repo', git=git)
    added = tracker.list_comments(child)[len(comments):]
    assert len(added) == 1 and len(added[0].body.splitlines()) == 1
    assert 'Waiting for the catalog owner' in added[0].body

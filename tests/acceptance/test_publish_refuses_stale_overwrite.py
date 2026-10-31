"""Locked: publishing cannot silently replace work changed since this plan read it.

The existing preread command is the public re-read in this contract. Storage of
that observed base belongs to the lane; no private baseline shape is prescribed.
"""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


MUTATIONS = (
    'ensure_labels', 'create_issue', 'update_issue', 'add_labels', 'remove_labels',
    'comment', 'close_issue', 'add_sub_issue', 'add_blocked_by', 'remove_blocked_by',
)


def reread(case, capsys):
    count = len(case.plan['issues']) + len(case.plan.get('edits', []))
    case.worker.steps = ['NONE'] * count
    before = len(case.worker.calls)
    capsys.readouterr()
    code = cli_exit(case.main, ['order', 'preread', str(case.path), '--repo-root', str(case.root), '--json'])
    output = capsys.readouterr()
    assert code == 0, output.out + output.err
    assert len(case.worker.calls) - before == count and case.worker.steps == []
    assert all(not row['unknowns'] for row in json.loads(output.out)['issues'])


def publish(case, capsys, *, approve=True):
    capsys.readouterr()
    before = len(case.worker.calls)
    argv = ['order', 'publish', str(case.path), '--repo-root', str(case.root)]
    if approve:
        argv.append('--approve')
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    assert len(case.worker.calls) == before, 'Publish consumes preread; it must not silently re-read and replace the base'
    assert case.gh == []
    return code, output.out + output.err


def public_facts(tracker):
    return {
        issue.number: {
            'issue': asdict(issue),
            'comments': [asdict(comment) for comment in tracker.list_comments(issue.number)],
            'labels': [asdict(event) for event in tracker.label_events(issue.number)],
            'children': [child.number for child in tracker.sub_issues(issue.number)],
            'blocked_by': [asdict(dep) for dep in tracker.blocked_by(issue.number)],
        }
        for issue in tracker.list_issues()
    }


@pytest.mark.parametrize('route', ['republish', 'edit'])
@pytest.mark.parametrize('changed', ['done_when', 'title_and_done_when'])
def test_publish_refuses_stale_issue_atomically_then_allows_explicit_reread(
    preread_case, publishing, monkeypatch, capsys, route, changed,
):
    """Reject blind overwrite, a warning after effects, a generic conflict without a diff,
    and a permanent ban that also refuses unchanged or explicitly re-read work.

    Both mapped children and explicit edits must be protected. Multiple changed
    fields must be diagnosed, and another issue in the plan must remain untouched.
    """
    case = preread_case
    publication_git = publishing[3]
    reread(case, capsys)
    code, output = publish(case, capsys)
    assert code == 0, output
    item = deepcopy(case.plan['issues'][0])
    number = next(issue.number for issue in case.tracker.list_issues() if issue.title == item['title'])
    if route == 'edit':
        case.plan = {
            **case.plan, 'id': case.plan['id'] + '-followup', 'issues': [],
            'edits': [{'number': number, **{key: item[key] for key in
                       ('goal', 'out_of_scope', 'done_when', 'acceptance_files')}}],
        }
        case.path.write_text(json.dumps(case.plan))
        reread(case, capsys)
        code, output = publish(case, capsys)
        assert code == 0, output

    # Control: read the current issue, then publish without an intervening edit.
    reread(case, capsys)
    code, output = publish(case, capsys)
    assert code == 0 and 'approved' in case.tracker.get_issue(number).labels, output
    reread(case, capsys)

    # The incident's external edit changes Done when, not a local test fixture.
    observed = case.tracker.get_issue(number)
    old_command = item['done_when'][0]
    new_command = old_command + ' -k receipt_locale'
    assert old_command in observed.body
    new_title = observed.title + ' revised on GitHub' if changed == 'title_and_done_when' else observed.title
    case.tracker.update_issue(number, title=new_title, body=observed.body.replace(old_command, new_command))
    expected_facts = public_facts(case.tracker)
    expected_events = deepcopy(case.events)
    expected_files = dict(publication_git.saved)
    expected_artifacts = {name: (case.root / name).read_bytes() for name in publication_git.saved}
    writes = []
    for name in MUTATIONS:
        original = getattr(case.tracker, name)

        def mutation(*args, _name=name, _original=original, **kwargs):
            writes.append((_name, args, kwargs))
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, mutation)

    code, diagnostic = publish(case, capsys, approve=False)
    assert code != 0, f'Stale publish must stop before overwriting GitHub issue #{number}'
    assert writes == [], f'A conflict was reported after tracker mutations: {writes}'
    assert case.events == expected_events and publication_git.saved == expected_files, 'A stale publish wrote publication files'
    assert {name: (case.root / name).read_bytes() for name in expected_artifacts} == expected_artifacts
    assert public_facts(case.tracker) == expected_facts, 'Refusal changed body, approval, comments or native edges'
    assert f'#{number}' in diagnostic, 'The conflict must identify the changed issue'
    assert 'Done when' in diagnostic and old_command in diagnostic and new_command in diagnostic, (
        'The conflict must show the changed field and the old/new check, not only a hash mismatch'
    )
    if changed == 'title_and_done_when':
        assert 'title' in diagnostic.lower() and observed.title in diagnostic and new_title in diagnostic

    # An explicit read refreshes the observed base; it does not require a new order
    # identity, disable the guard globally, or force a permanent refusal.
    before = len(case.worker.calls)
    reread(case, capsys)
    current_context = '\n'.join(call['prompt'] for call in case.worker.calls[before:])
    assert new_command in current_context, 'Re-read must give the agent the current GitHub check, not merely acknowledge a new hash'
    if changed == 'title_and_done_when':
        assert new_title in current_context
    code, output = publish(case, capsys)
    assert code == 0, f'An explicitly re-read plan must publish: {output}'
    assert 'approved' in case.tracker.get_issue(number).labels

"""Created targets join the current ownership snapshot without reopening existing facts."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from foragent import board, cli, inspect_actions, orch_scope, order, upstream_report
from foragent.config import Config
from foragent.ownership import OWNED_MARKER
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_board import item_values
from test_order import PublishingGit

NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def case(tmp_path):
    raw = FakeTracker(actor='alice', login='alice', now=lambda: NOW)
    raw.add_issue(1, 'Existing own issue', labels=['by:alice'])
    raw.add_issue(2, 'Existing foreign issue', labels=['by:bob'])
    reads = []
    raw.authenticated_login = lambda: reads.append('login') or 'alice'
    project = board.ensure(raw, 'owner/repo')
    config = Config(repo='owner/repo', legacy_owner='alice', board=project, skill_libraries=[],
                    approvers=['alice', 'bob'])
    store = StateStore(tmp_path / 'state', now=lambda: NOW)
    store.refresh_local_issues(raw.list_issues(), [], {}, NOW)
    ctx = RunContext('owner/repo', tmp_path, raw, store, config, None, lambda: NOW)
    ctx.tracker.comment(1, 'Freeze the snapshot')
    return SimpleNamespace(raw=raw, ctx=ctx, store=store, root=tmp_path, project=project, reads=reads)


@pytest.mark.parametrize('cached_missing', [False, True])
@pytest.mark.parametrize('effect', ['approve', 'remove_label', 'comment', 'update', 'close', 'board',
                                   'sub_issue', 'dependency', 'pr'])
def test_new_owned_issue_accepts_each_followup_effect(case, effect, cached_missing):
    ctx, raw = case.ctx, case.raw
    if cached_missing:
        assert orch_scope.scope(ctx).for_number(3) is not None
    number = ctx.tracker.create_issue('Created now', 'Body', ['by:alice', 'ready'])
    assert number == 3
    if effect == 'approve':
        ctx.tracker.add_labels(number, ['approved'])
        assert 'approved' in raw.get_issue(number).labels
    elif effect == 'remove_label':
        ctx.tracker.remove_labels(number, ['ready'])
        assert 'ready' not in raw.get_issue(number).labels
    elif effect == 'comment':
        ctx.tracker.comment(number, 'After creation')
        assert [c.body for c in raw.list_comments(number)] == ['After creation']
    elif effect == 'update':
        ctx.tracker.update_issue(number, title='Updated', body='Updated body')
        assert (raw.get_issue(number).title, raw.get_issue(number).body) == ('Updated', 'Updated body')
    elif effect == 'close':
        ctx.tracker.close_issue(number)
        assert raw.get_issue(number).state == 'closed'
    elif effect == 'board':
        board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, number, 'main', 's')
        board.sync_status(ctx, number, 'Approved')
        assert item_values(raw, case.project, number)['Status'] == board.board_column('Approved')
    elif effect == 'sub_issue':
        child = ctx.tracker.create_issue('New child', '', ['by:alice'])
        ctx.tracker.add_sub_issue(number, child)
        assert [item.number for item in raw.sub_issues(number)] == [child]
    elif effect == 'dependency':
        ctx.tracker.add_blocked_by(number, 1)
        assert [dep.number for dep in raw.blocked_by(number)] == [1]
    else:
        pr = ctx.tracker.create_pr(head=f'foragent/issue-{number}', base='main', title='New PR', body='')
        ctx.tracker.comment(pr, 'After creating the PR')
        assert [c.body for c in raw.list_comments(pr)] == ['After creating the PR']
    assert case.reads == ['login'], "Creating targets must reuse this operation's identity"


@pytest.mark.parametrize('legacy,labels,body', [
    ('alice', ['by:bob'], ''), ('alice', ['by:alice', 'by:bob'], ''),
    ('bob', [], ''), ('alice', [], OWNED_MARKER), (None, ['by:bob'], ''),
])
def test_creation_does_not_override_the_normal_owner_rule(case, legacy, labels, body):
    from dataclasses import replace

    ctx = case.ctx
    ctx.config = replace(ctx.config, legacy_owner=legacy)
    # A previously permitted unknown singleton number must not override new owner facts.
    orch_scope.scope(ctx).for_number(3)
    number = ctx.tracker.create_issue('Created for another owner', body, labels)
    before = case.raw.get_issue(number)
    ctx.tracker.add_labels(number, ['approved'])
    ctx.tracker.comment(number, 'Not authorized')
    assert case.raw.get_issue(number) == before and case.raw.list_comments(number) == []


def test_new_creation_does_not_refresh_preexisting_issue_ownership(case):
    raw, tracker = case.raw, case.ctx.tracker
    raw.remove_labels(2, ['by:bob'])
    raw.add_labels(2, ['by:alice'])
    case.store.refresh_local_issues(raw.list_issues(), [], {}, NOW)  # Another observation updated the backing record.
    tracker.get_issue(2)  # Ordinary reads cannot reopen the operation's frozen owner decision.
    number = tracker.create_issue('New own issue', '', ['by:alice'])
    tracker.comment(number, 'Allowed')
    tracker.comment(2, 'Still foreign in this operation')
    assert [c.body for c in raw.list_comments(number)] == ['Allowed']
    assert raw.list_comments(2) == []


@pytest.mark.parametrize('publisher', ['alice', 'bob'])
@pytest.mark.parametrize('drop_owner_label,fresh_cache_edit', [(False, False), (True, False), (False, True)])
def test_team_order_cli_approves_and_links_every_new_child(case, monkeypatch, capsys, drop_owner_label, fresh_cache_edit, publisher):
    ctx, raw = case.ctx, case.raw
    raw.actor = publisher
    raw.authenticated_login = lambda: publisher
    edited = 1 if publisher == 'alice' else 2
    plan = {'id': 'new-owned-targets', 'title': 'Three own tasks', 'summary': 'Publish owned work',
            'spec_markdown': '# Owned tasks\n', 'issues': [
                {'key': key, 'title': key, 'goal': f'Create {key}.', 'out_of_scope': 'Other work.',
                 'lane': 'main', 'size': 's', 'done_when': ['true'], 'files': [f'{key}.py'],
                 'blocked_by': ['one'] if key == 'two' else []}
                for key in ['one', 'two', 'three']]}
    if fresh_cache_edit:
        plan['edits'] = [{'number': edited, 'goal': 'Revise existing own work', 'out_of_scope': 'Other work.',
                          'done_when': ['true']}]
    publication_store = StateStore(case.root / 'fresh-state', now=lambda: NOW) if fresh_cache_edit else case.store
    path = case.root / 'plan.json'
    path.write_text(json.dumps(plan))
    (case.root / '.foragent.yml').write_text('repo: owner/repo\nlegacy_owner: alice\n')
    git = PublishingGit(case.root, [])
    monkeypatch.setattr(cli, 'GitRepo', lambda root: git)
    monkeypatch.setattr(cli, 'load_config', lambda root: ctx.config)
    monkeypatch.setattr(cli, 'GhTracker', lambda repo: raw)
    monkeypatch.setattr(cli.StateStore, 'for_repo', lambda repo: publication_store)
    monkeypatch.setattr(cli, '_open_lane_view', lambda *a: None)
    monkeypatch.setattr(order, 'kick_tick', lambda *a: None)
    monkeypatch.setattr(order, 'require_clean_preread', lambda *a: None)
    if drop_owner_label:
        create = raw.create_issue

        def drop_label(title, body, labels):
            number = create(title, body, labels)
            if title == 'two':
                raw.remove_labels(number, [f'by:{publisher}'])  # The server did not apply the requested label.
            return number

        monkeypatch.setattr(raw, 'create_issue', drop_label)
    code = cli.main(['order', 'publish', str(path), '--repo-root', str(case.root), '--approve', '--urgent'])
    if drop_owner_label:
        assert code != 0
        failed, = [item for item in raw.list_issues() if item.title == 'two']
        assert failed.state == 'closed' and 'foragent:plan-child' not in failed.body
        assert not any('approved' in item.labels for item in raw.list_issues())
        assert f'by:{publisher}' in capsys.readouterr().err
        return
    assert code == 0
    children = [item for item in raw.list_issues() if 'foragent:plan-child' in item.body]
    parent, = [item for item in raw.list_issues() if 'foragent:plan id=' in item.body]
    assert len(children) == 3 and {item.number for item in raw.sub_issues(parent.number)} == {c.number for c in children}
    by_title = {item.title: item.number for item in children}
    assert [dep.number for dep in raw.blocked_by(by_title['two'])] == [by_title['one']]
    for item in children:
        assert {f'by:{publisher}', 'approved'} <= item.labels
        assert item_values(raw, case.project, item.number)['Status'] == board.board_column('Approved')
    if fresh_cache_edit:
        assert 'Revise existing own work' in raw.get_issue(edited).body and 'approved' in raw.get_issue(edited).labels
    assert capsys.readouterr().err == ''


def test_feedback_creation_can_be_followed_by_owned_writes(case):
    ctx = inspect_actions.InspectContext(case.ctx.tracker, case.store, 'owner/repo', case.ctx.config, lambda: NOW)
    ctx._scope = case.ctx._scope
    number = inspect_actions.feedback(ctx, 'Feedback', 'Details', about=1)
    case.ctx.tracker.comment(number, 'Follow-up')
    assert [c.body for c in case.raw.list_comments(number)] == ['Follow-up']


def test_wrapped_upstream_creation_can_be_followed_by_owned_writes(case, monkeypatch):
    monkeypatch.setattr(case.raw, 'search_issues', lambda query: {'items': [], 'total_count': 0}, raising=False)
    report = {'title': 'Report', 'body': 'Details', 'signature': 'fa-sig-' + 'b' * 12}
    report['id'] = upstream_report._report_id(report['title'], report['body'], report['signature'])
    action, url = upstream_report.send(case.store, report, case.ctx.tracker)
    number = int(url.rsplit('/', 1)[-1])
    case.ctx.tracker.comment(number, 'Follow-up')
    assert action == 'create' and [c.body for c in case.raw.list_comments(number)] == ['Follow-up']


def test_creation_registers_its_returned_facts_without_an_extra_issue_read(case, monkeypatch):
    reads = []
    original = case.raw.get_issue
    monkeypatch.setattr(case.raw, 'get_issue', lambda n: reads.append(n) or original(n))
    number = case.ctx.tracker.create_issue('Fresh', 'Body', ['by:alice'])
    assert reads == [] and case.reads == ['login']
    case.ctx.tracker.comment(number, 'Registered')
    assert [c.body for c in case.raw.list_comments(number)] == ['Registered']


def test_first_creation_preserves_lazy_capture_of_existing_entry_reads(tmp_path):
    raw = FakeTracker(login='alice', now=lambda: NOW)
    raw.add_issue(1, 'Existing own edit', labels=['by:alice'])
    store = StateStore(tmp_path / 'state', now=lambda: NOW)
    ctx = RunContext('owner/repo', tmp_path, raw, store, Config(legacy_owner='alice'), None, lambda: NOW)
    number = ctx.tracker.create_issue('New order record', '', ['by:alice'])
    ctx.tracker.get_issue(1)  # The command's normal input read, before its first gated write.
    ctx.tracker.comment(1, 'Apply the existing edit')
    ctx.tracker.comment(number, 'Update the new record')
    assert [c.body for c in raw.list_comments(1)] == ['Apply the existing edit']
    assert [c.body for c in raw.list_comments(number)] == ['Update the new record']


def test_refresh_keeps_observed_pr_targets_usable_in_the_listing_snapshot(tmp_path):
    raw = FakeTracker(login='alice', now=lambda: NOW)
    raw.add_issue(1, 'Own issue', labels=['by:alice'])
    pr = raw.create_pr(head='foragent/issue-1', base='main', title='Original', body='')
    store = StateStore(tmp_path / 'state', now=lambda: NOW)
    ctx = RunContext('owner/repo', tmp_path, raw, store, Config(legacy_owner='alice'), None, lambda: NOW)
    ctx.tracker.refresh_issues()
    ctx.tracker.update_pr(pr, base='main', title='Updated from this snapshot', body='')
    assert raw.created_prs[0].title == 'Updated from this snapshot'

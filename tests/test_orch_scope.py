"""The shared person boundary for this factory's effects (#1004)."""
from types import SimpleNamespace

from foragent import orch_scope
from foragent.config import Config
from foragent.ownership import OWNED_MARKER
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, RecordingTracker, TrackerError


def issue(*labels, number=7, body=""):
    return Issue(number, "Work", body, frozenset(labels), "open")


class CountingTracker(FakeTracker):
    def __init__(self, login="alice"):
        super().__init__(login=login)
        self.identity_reads = 0

    def authenticated_login(self, *, refresh=False):
        self.identity_reads += 1
        return super().authenticated_login(refresh=refresh)


def test_singleton_unlabelled_work_needs_no_identity_read():
    tracker = CountingTracker()
    assert orch_scope.foreign_reason(issue(), Config(), tracker) is None
    assert tracker.identity_reads == 0


def test_own_work_is_allowed_and_foreign_work_names_both_people():
    tracker = CountingTracker("alice")
    assert orch_scope.foreign_reason(issue("by:Alice"), Config(), tracker) is None
    refusal = orch_scope.foreign_reason(issue("by:bob"), Config(), tracker)
    assert refusal == "#7 is worked by bob; this factory is alice."


def test_legacy_owner_decides_unlabelled_work():
    config = Config(legacy_owner="alice")
    assert orch_scope.foreign_reason(issue(), config, CountingTracker("alice")) is None
    assert "alice" in orch_scope.foreign_reason(issue(), config, CountingTracker("bob"))


def test_doubt_refuses_ambiguous_unconfirmed_and_unknown_identity():
    tracker = CountingTracker("alice")
    assert orch_scope.foreign_reason(issue("by:alice", "by:bob"), Config(), tracker) is not None
    assert orch_scope.foreign_reason(issue(body=OWNED_MARKER), Config(), tracker) is not None
    nobody = FakeTracker(login="")
    assert orch_scope.foreign_reason(issue("by:alice"), Config(), nobody) is not None
    assert orch_scope.factory_login(nobody) is None


def test_scope_reads_identity_once_per_tick():
    tracker = CountingTracker("alice")
    scope = orch_scope.Scope(SimpleNamespace(tracker=tracker, config=Config()))
    assert [scope.owns(issue("by:alice", number=n)) for n in (1, 2)] == [True, True]
    assert not scope.owns(issue("by:bob"))
    assert tracker.identity_reads == 1


def test_merge_resolves_the_pr_to_its_issue_before_comparing():
    tracker = FakeTracker(login="alice")
    tracker.add_issue(3, "Bob's", "", labels=["by:bob"])
    pr = tracker.create_pr(head="foragent/issue-3", base="main", title="t", body="b")
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    assert pr != 3
    assert orch_scope.merge_check(ctx, 3) == "#3 is worked by bob; this factory is alice."
    assert orch_scope.refuse_verb(ctx, "list", None) is None


def test_a_failed_owner_read_refuses_the_verb_instead_of_allowing_it():
    class Failing(FakeTracker):
        def get_issue(self, n):
            raise TrackerError("HTTP 502")

    ctx = SimpleNamespace(tracker=Failing(login="alice"), config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    refusal = orch_scope.refuse_verb(ctx, "retry", 1)
    assert refusal is not None and "#1" in refusal and "502" in refusal
    assert orch_scope.may_write(ctx, 1) is False


def flush_case(tmp_path, *issues, legacy=None):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 31, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: now, config=Config(legacy_owner=legacy))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, now)  # The tick's listing.
    return store, tracker, recorder, now



def test_refused_replay_moves_aside_and_journals_owner_and_action(tmp_path):
    import json

    store, tracker, recorder, _ = flush_case(tmp_path, (1, ['by:bob']), (2, ['by:alice']))
    store.queue_github_write(1, 'comment', body='Foreign')
    store.queue_github_write(2, 'comment', body='Mine')
    recorder.flush_outbox()
    recorder.flush_outbox()
    assert tracker.list_comments(1) == []
    assert [c.body for c in tracker.list_comments(2)] == ['Mine']
    assert store.github_outbox() == []
    [refused] = store.refused_github_writes()
    assert refused['issue'] == 1 and refused['body'] == 'Foreign' and 'bob' in refused['refused']
    [event] = [json.loads(line) for line in (store.root / 'orch-events.jsonl').read_text().splitlines()]
    assert (event['kind'], event['issue'], event['action'], event['owner']) == ('ownership-refused', 1, 'comment', 'bob')
    recorder.comment(2, 'Later')
    assert [c.body for c in tracker.list_comments(2)] == ['Mine', 'Later']


import pytest


@pytest.mark.parametrize('action', ['comment', 'add_labels', 'remove_labels', 'update_issue', 'close_issue'])
@pytest.mark.parametrize('pending', [False, True])
def test_foreign_writes_are_refused_before_send_queue_or_local_changes(tmp_path, action, pending):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ['by:bob']), (2, ['by:alice']))
    if pending:
        store.queue_github_write(2, 'comment', body='Older')
    before = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    issue_before = tracker.get_issue(1)
    if action == 'update_issue':
        recorder.update_issue(1, title='Changed', body='Changed')
    else:
        payload = ['ready'] if action.endswith('labels') else 'Changed'
        getattr(recorder, action)(1, payload)
    assert tracker.get_issue(1) == issue_before and tracker.list_comments(1) == []
    assert {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()} == before


def test_scope_keeps_first_snapshot_and_the_next_operation_observes_relabel(tmp_path, monkeypatch):
    store, tracker, recorder, now = flush_case(tmp_path, (1, ['by:alice']), legacy='alice')
    assert orch_scope.check(recorder._context, 1, tracker.get_issue(1)) is None
    tracker.remove_labels(1, ['by:alice'])
    tracker.add_labels(1, ['by:bob'])
    monkeypatch.setattr(orch_scope, 'current_issue', lambda *a, **k: pytest.fail('No ownership reread inside an operation'))
    recorder.comment(1, 'Same operation')
    assert [c.body for c in tracker.list_comments(1)] == ['Same operation']
    store.refresh_local_issues([tracker.get_issue(1)], [], {}, now)
    recorder._context._scope = orch_scope.Scope(recorder._context)
    recorder.comment(1, 'Next operation')
    assert [c.body for c in tracker.list_comments(1)] == ['Same operation']


def test_missing_linked_issue_does_not_block_other_queued_writes(tmp_path, monkeypatch):
    store, tracker, recorder, _ = flush_case(tmp_path, (2, ['by:alice']), legacy='alice')
    pr = tracker.create_pr(head='foragent/issue-9', base='main', title='Missing issue', body='')
    store.update_local_issue(9, pr_number=pr)  # Only PR metadata; issue #9 has never been observed.
    store.queue_github_write(pr, 'comment', body='No owner evidence')
    store.queue_github_write(2, 'comment', body='Mine')
    original = tracker.get_issue

    def missing(n):
        if n == 9:
            raise TrackerError('HTTP 404: Not Found')
        return original(n)

    monkeypatch.setattr(tracker, 'get_issue', missing)
    recorder.flush_outbox()
    assert tracker.list_comments(pr) == [] and store.github_outbox() == []
    assert [c.body for c in tracker.list_comments(2)] == ['Mine']
    [refused] = store.refused_github_writes()
    assert refused['issue'] == pr and 'absent' in refused['refused']


def test_pr_writes_use_the_linked_parent_ownership(tmp_path):
    from foragent.tracker import WriteRefused

    store, tracker, recorder, _ = flush_case(tmp_path, (1, ['by:alice']), (9, ['by:bob']), legacy='alice')
    pr = tracker.create_pr(head='foragent/batch-9', base='main', title='Bob', body='')
    store.update_local_issue(9, pr_number=pr)
    with pytest.raises(WriteRefused, match='bob'):
        recorder.create_pr(head='foragent/batch-9', base='main', title='Bob', body='')
    for effect in (lambda: recorder.update_pr(pr, base='main', title='Changed', body=''),
                   lambda: recorder.close_pr(pr),
                   lambda: recorder.merge_pr(pr, expected_head_sha='head')):
        with pytest.raises(WriteRefused, match='bob'):
            effect()
    recorder.comment(1, 'Own child continues')
    assert [c.body for c in tracker.list_comments(1)] == ['Own child continues']
    assert tracker.merged_prs == [] and len(tracker.created_prs) == 1


def test_board_writes_use_the_same_snapshot_gate(tmp_path):
    from foragent.board import Project

    _, _, recorder, _ = flush_case(tmp_path, (1, ['by:bob']), legacy='alice')
    recorder.graphql = lambda *a, **k: pytest.fail('No board reads or mutations for a foreign card')
    Project(recorder, 'owner/repo', 'board').set_values(1, {'Status': 'Done'})


def test_question_uses_entry_orderer_without_another_issue_read(tmp_path, monkeypatch):
    _, tracker, recorder, _ = flush_case(tmp_path, (1, ['by:alice']), legacy='alice')
    ctx = recorder._context
    assert orch_scope.check(ctx, 1, tracker.get_issue(1)) is None
    monkeypatch.setattr(tracker, 'get_issue', lambda n: pytest.fail('Use the entry snapshot'))
    assert orch_scope.ask_orderer(ctx, 1, 'Choose.') == '@alice Choose.'


def test_parent_reads_foreign_children_but_writes_only_owned_parents(tmp_path, monkeypatch):
    from foragent import board, board_parents

    store, tracker, recorder, now = flush_case(tmp_path, (1, ['by:alice']), (2, ['by:bob']),
                                              (3, ['by:bob']), legacy='alice')
    tracker.issues[2] = Issue(2, 'Child', '', frozenset(['by:bob']), 'closed', state_reason='not_planned')
    tracker.set_parent(2, 1)
    tracker.set_parent(2, 3)
    store.update_local_issue(1, children=[2])
    store.update_local_issue(3, children=[2])
    monkeypatch.setattr(tracker, 'sub_issues', lambda n: [tracker.get_issue(2)])
    moved = []
    monkeypatch.setattr(board, 'sync_status', lambda ctx, n, state, **kw: moved.append((n, state)))
    ctx = recorder._context
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert tracker.get_issue(1).state == 'closed' and tracker.get_issue(3).state == 'open'
    assert moved == [(1, 'Done')]
    assert '#2' in tracker.list_comments(1)[0].body


@pytest.mark.parametrize('owner', ['alice', 'bob'])
def test_chained_pr_targets_use_the_original_issue_snapshot(tmp_path, monkeypatch, owner):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, [f'by:{owner}']), legacy='alice')
    first = tracker.create_pr(head='foragent/issue-1', base='main', title='First', body='')
    second = tracker.create_pr(head=f'foragent/issue-{first}', base='main', title='Second', body='')
    store.update_local_issue(1, pr_number=first)
    store.update_local_issue(first, pr_number=second)
    monkeypatch.setattr(orch_scope, 'current_issue', lambda *a, **k: pytest.fail('Resolve from the snapshot'))
    recorder.comment(second, 'Chain target')
    snapshot = orch_scope.scope(recorder._context)
    canonical = snapshot.issue(second)
    assert canonical is not None and canonical.number == 1
    assert [c.body for c in tracker.list_comments(second)] == (['Chain target'] if owner == 'alice' else [])
    if owner == 'bob':
        assert 'bob' in snapshot.for_number(second)


def test_cyclic_pr_links_refuse_without_writes_or_looping(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ['by:alice']), (2, ['by:alice']), legacy='alice')
    store.update_local_issue(1, pr_number=2)
    store.update_local_issue(2, pr_number=1)
    before = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    recorder.comment(1, 'No canonical issue')
    snapshot = orch_scope.scope(recorder._context)
    refusal = snapshot.for_number(1)
    assert refusal is not None and 'cyclic' in refusal and snapshot.issue(1) is None
    assert tracker.list_comments(1) == []
    assert {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()} == before

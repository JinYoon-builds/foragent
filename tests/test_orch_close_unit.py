"""Superseded closure boundaries using the same real local history as the CLI lock."""

from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'acceptance'))
from conftest import git_command
from foragent.tracker import GhTracker, TrackerError
from test_orch_close_superseded import (
    BRANCH, OWN, approve, assert_nothing_changed, base_files, base_tip, close, superseded_case,
)


def test_unapproved_issue_closes_without_touching_base(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    assert case.store.load_approval(2) is None
    assert close(case, n=2, by=1) == 0
    assert case.tracker.get_issue(2).state == 'closed'
    assert len(case.tracker.list_comments(2)) == 1
    assert_nothing_changed(case)


@pytest.mark.parametrize('evidence', ['label', 'record'])
def test_running_refusal_accepts_either_evidence(git_history, tmp_path, monkeypatch, evidence):
    case = superseded_case(git_history, tmp_path, monkeypatch, running=True)
    if evidence == 'label':
        case.store.finish_run(1, outcome='error')
    else:
        case.tracker.remove_labels(1, ['running'])
    assert close(case) == 1
    assert_nothing_changed(case)


def test_confirmed_merge_time_protects_even_nonmerged_outcome(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))
    case.store.record_merge(1, case.clock[0])
    case.store.finish_run(1, outcome='error')
    assert close(case) == 1
    assert_nothing_changed(case)


def test_repeat_and_partial_retry_have_one_commit_and_comment(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    original = case.tracker.close_pr
    calls = []

    def failing(*args, **kwargs):
        calls.append(True)
        raise TrackerError('PR close unavailable')

    monkeypatch.setattr(case.tracker, 'close_pr', failing)
    assert close(case) == 1
    assert calls == [True]
    removed_tip = base_tip(case)
    assert OWN not in base_files(case)
    assert case.tracker.get_issue(1).state == 'open'
    assert len(case.tracker.list_comments(1)) == case.before['comments']
    monkeypatch.setattr(case.tracker, 'close_pr', original)
    assert close(case) == 0
    assert close(case) == 0
    assert base_tip(case) == removed_tip
    assert len(case.tracker.list_comments(1)) == case.before['comments'] + 1
    assert case.tracker.deleted_branches == [BRANCH]


def test_removal_keeps_checkout_and_index_and_uses_exact_lease(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    before = {command: git_command(case.root, *command)
              for command in [('rev-parse', 'HEAD'), ('write-tree',), ('status', '--porcelain')]}
    original = case.git._call
    pushes = []

    def call(args):
        if args[0] == 'push':
            pushes.append(args)
        return original(args)

    monkeypatch.setattr(case.git, '_call', call)
    assert close(case) == 0
    assert len(pushes) == 1
    assert f"--force-with-lease=refs/heads/main:{case.before['tip']}" in pushes[0]
    assert {command: git_command(case.root, *command) for command in before} == before


def test_retry_after_comment_sent_does_not_comment_twice(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    original = case.tracker.close_issue

    def fail_after_comment(n, comment=None, **kwargs):
        if comment is not None:
            case.tracker.comment(n, comment)
        raise TrackerError('issue close failed after comment')

    monkeypatch.setattr(case.tracker, 'close_issue', fail_after_comment)
    assert close(case) == 1
    assert case.tracker.get_issue(1).state == 'open'
    monkeypatch.setattr(case.tracker, 'close_issue', original)
    assert close(case) == 0
    assert len(case.tracker.list_comments(1)) == case.before['comments'] + 1
    assert case.tracker.deleted_branches == [BRANCH]


@pytest.mark.parametrize("already_closed", [True, False])
def test_preclosed_unmerged_issue_still_cleans_up(git_history, tmp_path, monkeypatch, already_closed):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    original_close = case.tracker.close_issue
    if already_closed:
        original_close(1)

    def gh_run(args, input=None):
        # gh issue close returns before processing --comment on a closed issue.
        assert args[:3] == ["issue", "close", "1"]
        if not already_closed:
            original_close(1)  # Concurrent closer after the command's initial read.
        assert case.tracker.get_issue(1).state == "closed"
        return b""  # No comment is posted, even when the caller passed --comment.

    monkeypatch.setattr(case.tracker, "close_issue", GhTracker("owner/repo", run=gh_run).close_issue)
    assert close(case) == 0
    assert close(case) == 0  # Repeated cleanup must not add a second comment.
    assert OWN not in base_files(case)
    assert case.tracker.get_issue(case.pr).state == 'closed'
    assert case.tracker.deleted_branches == [BRANCH]
    assert len(case.tracker.list_comments(1)) == case.before['comments'] + 1


@pytest.mark.parametrize("branch", ["foragent/issue-1", "foragent/batch-1"])
def test_remote_merge_without_run_record_refuses_every_effect(git_history, tmp_path, monkeypatch, branch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    # A GitHub merge of an earlier PR, with no local merged run or merge timestamp.
    remote = case.tracker.create_pr(head=branch, base="main", title="Earlier PR", body="Closes #1")
    case.tracker.created_prs = [replace(pr, merged_at=case.clock[0].isoformat()) if pr.number == remote else pr
                                for pr in case.tracker.created_prs]
    case.tracker.close_issue(remote)
    assert not any(run.number == 1 and run.outcome == "merged" for run in case.store.read_history())
    assert not any(number == 1 for number, _ in case.store.merge_times())
    assert close(case) == 1
    assert_nothing_changed(case)


@pytest.mark.parametrize("branch", ["foragent/issue-10", "foragent/batch-10"])
def test_remote_merged_owner_preserves_shared_lock_without_run_record(git_history, tmp_path, monkeypatch, branch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_issue(10, "Remote merged owner", case.tracker.get_issue(1).body)
    approve(case, 10, [OWN])
    remote = case.tracker.create_pr(head=branch, base="main", title="Owner PR", body="Closes #10")
    case.tracker.created_prs = [replace(pr, merged_at=case.clock[0].isoformat()) if pr.number == remote else pr
                                for pr in case.tracker.created_prs]
    case.tracker.close_issue(remote)
    assert not any(run.number == 10 for run in case.store.read_history())
    assert close(case) == 0
    assert base_tip(case) == case.before["tip"]
    assert base_files(case) == case.before["files"]
    assert case.tracker.get_issue(1).state == "closed"
    assert case.tracker.get_issue(case.pr).state == "closed"


def _shared_close_case():
    from foragent.tracker import FakeTracker

    tracker = FakeTracker()
    issue = tracker.create_issue('Fix', 'body', [])
    other = tracker.create_issue('Other', 'body', [])
    pr = tracker.create_pr(head=f'foragent/issue-{issue}', base='main', title='Fix', body='')
    other_pr = tracker.create_pr(head=f'foragent/issue-{other}', base='main', title='Other', body='')
    return tracker, issue, pr, other_pr


def test_shared_close_takes_the_pr_and_branch_first_then_the_issue_once():
    from foragent.orch_close import close_with_pr

    tracker, issue, pr, other_pr = _shared_close_case()
    order = []
    close_pr, close_issue = tracker.close_pr, tracker.close_issue
    tracker.close_pr = lambda n, *a, **k: (order.append(('pr', n)), close_pr(n, *a, **k))[1]
    tracker.close_issue = lambda n, *a, **k: (order.append(('issue', n, a, k)), close_issue(n, *a, **k))[1]
    close_with_pr(tracker, issue, 'done', reason='completed')
    assert order == [('pr', pr), ('issue', issue, ('done',), {'reason': 'completed'})]
    assert tracker.deleted_branches == [f'foragent/issue-{issue}']
    assert tracker.get_issue(other_pr).state == 'open'
    close_with_pr(tracker, issue, 'done', reason='completed')
    assert order == [('pr', pr), ('issue', issue, ('done',), {'reason': 'completed'})]
    assert tracker.deleted_branches == [f'foragent/issue-{issue}']


def test_shared_close_retry_after_failed_issue_close_finishes_without_a_second_pr_close():
    from foragent.orch_close import close_with_pr

    tracker, issue, pr, _ = _shared_close_case()
    close_issue = tracker.close_issue

    def fail(*_args, **_kwargs):
        raise TrackerError('HTTP 502')

    tracker.close_issue = fail
    with pytest.raises(TrackerError):
        close_with_pr(tracker, issue)
    assert tracker.get_issue(pr).state == 'closed' and tracker.get_issue(issue).state == 'open'
    tracker.close_issue = close_issue
    close_with_pr(tracker, issue)
    assert tracker.get_issue(issue).state == 'closed'
    assert tracker.deleted_branches == [f'foragent/issue-{issue}']


def test_shared_close_without_a_pr_closes_only_the_issue():
    from foragent.orch_close import close_with_pr

    tracker, issue, pr, _ = _shared_close_case()
    tracker.close_pr(pr, delete_branch=False)
    close_with_pr(tracker, issue)
    assert tracker.get_issue(issue).state == 'closed'
    assert tracker.deleted_branches == []

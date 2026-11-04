"""Superseded closure boundaries using the same real local history as the CLI lock."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'acceptance'))
from conftest import git_command
from foragent.tracker import TrackerError
from test_orch_close_superseded import (
    BRANCH, OWN, assert_nothing_changed, base_files, base_tip, close, superseded_case,
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


def test_preclosed_unmerged_issue_still_cleans_up(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    case.tracker.close_issue(1)
    assert close(case) == 0
    assert OWN not in base_files(case)
    assert case.tracker.get_issue(case.pr).state == 'closed'
    assert case.tracker.deleted_branches == [BRANCH]
    assert len(case.tracker.list_comments(1)) == case.before['comments'] + 1

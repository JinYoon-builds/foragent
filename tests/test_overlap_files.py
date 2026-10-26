"""Admission keeps legacy lanes while counting actual shared paths across known runs."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.issue import body_hash, parse_body
from foragent.overlap_files import can_start, claims, measure
from foragent.state import StateStore
from runner_support import import_index


@pytest.fixture
def context(git_history, tmp_path):
    return SimpleNamespace(config=Config(), git=GitRepo(git_history.clone), repo_root=git_history.clone,
                           now=lambda: BASE_TIME, store=StateStore(tmp_path / 'state', now=lambda: BASE_TIME))


def reserve(context, number, files, *, lane='main', exclusive=(), running=True, start=None):
    body = '<!-- foragent:v1 -->\n'
    if exclusive:
        body += 'exclusive:\n' + ''.join(f'  - {path}\n' for path in exclusive)
    body += '## Files\n' + ''.join(f'- {path}\n' for path in files)
    context.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                approved_by='human', approved_at=BASE_TIME.isoformat())
    if not running:
        return
    context.store.start_run(number, branch=f'foragent/issue-{number}',
                            worktree=context.store.worktree_path(number), lane=lane)
    if start is not None:
        context.store.update_run(number, started_at=start.isoformat())


def edit(context, number, files, *, commit=False):
    run = context.store.get_run(number)
    path = context.store.worktree_path(number)
    if not path.exists():
        context.git.add_worktree(path, run.branch, 'origin/main')
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            target.unlink()
        else:
            target.write_text(content)
    if commit:
        context.git.at(path).stage_changes()
        import_index(path, 'Offline implementation')
    return path


def test_broad_glob_counts_shared_paths_rather_than_all_matching_runs(context):
    reserve(context, 1, ['src/a.py'])
    reserve(context, 2, ['src/b.py'], lane='api')
    reserve(context, 3, ['src/**'], running=False)
    assert can_start(context, 'view', 3)
    reserve(context, 4, ['src/[ab].py'], lane='other')
    assert not can_start(context, 'view', 3)


@pytest.mark.parametrize('missing', [False, True])
def test_unknown_and_empty_files_hold_only_their_own_lane(context, missing):
    reserve(context, 1, [])
    if missing:
        context.store.clear_approval(1)
    reserve(context, 2, ['src/a.py'], running=False)
    assert not can_start(context, 'main', 2)
    assert can_start(context, 'api', 2)


def test_empty_candidate_takes_a_free_lane_without_cross_lane_counting(context):
    reserve(context, 1, ['src/a.py'])
    reserve(context, 2, [], running=False)
    assert not can_start(context, 'main', 2)
    assert can_start(context, 'api', 2)


def test_actual_paths_are_literal_and_include_deletions(context, git_history):
    git_history.update({'deleted.py': 'old\n'})
    context.git.fetch('origin', 'main')
    context.config = replace(context.config, overlap_per_file=1)
    reserve(context, 1, ['**'])
    edit(context, 1, {'deleted.py': None, 'src/a[1].py': 'new\n'}, commit=True)
    run = claims(context)[0][0].run
    assert run.effective_files == ['deleted.py', 'src/a[1].py']
    reserve(context, 2, ['src/a1.py'], running=False)
    assert can_start(context, 'main', 2)
    reserve(context, 3, ['deleted.py'], running=False)
    assert not can_start(context, 'main', 3)


def test_unavailable_worktree_retains_the_last_measured_claim(context):
    reserve(context, 1, ['src/a.py', 'src/b.py'])
    edit(context, 1, {'src/a.py': 'new\n'}, commit=True)
    before = claims(context)[0][0].run
    context.git.remove_worktree(context.store.worktree_path(1))
    after = measure(context, before, parse_body(context.store.load_approval(1).body))
    assert after.effective_files == before.effective_files == ['src/a.py']


@pytest.mark.parametrize('previous', ['unmeasured', 'uncommitted', 'committed'])
@pytest.mark.parametrize('failure', [
    ['diff', '--cached'], ['diff', '--name-only'], ['ls-files', '--others'],
], ids=['staged-diff', 'working-diff', 'untracked-files'])
def test_failed_measurement_keeps_the_previous_snapshot_whole(context, monkeypatch, previous, failure):
    reserve(context, 1, ['src/a.py', 'src/b.py'])
    if previous != 'unmeasured':
        edit(context, 1, {'src/a.py': 'old\n'}, commit=previous == 'committed')
        claims(context)
    before = context.store.get_run(1)
    edit(context, 1, {'src/a.py': 'new\n', 'src/b.py': 'new\n'}, commit=True)
    call = GitRepo._call
    reached = []

    def failing_call(git, args):
        if args[:len(failure)] == failure:
            reached.append(args)
            raise GitError('Worktree disappeared during measurement')
        return call(git, args)

    with monkeypatch.context() as patch:
        patch.setattr(GitRepo, '_call', failing_call)
        after = measure(context, before, parse_body(context.store.load_approval(1).body), 'main')
        assert after == before == context.store.get_run(1)
        known, unknown = claims(context)
        assert len(known) == 1 and unknown == []
        assert known[0].run == before
        assert reached
    assert claims(context)[0][0].run.effective_files == ['src/a.py', 'src/b.py']


def test_legacy_committed_record_with_none_effective_files_uses_declarations(context):
    reserve(context, 1, ['src/*.py'])
    context.store.update_run(1, files_committed=True, effective_files=None)
    known, unknown = claims(context)
    assert unknown == []
    assert known[0].files == ['src/*.py']
    reserve(context, 2, ['src/a.py'], running=False)
    context.config = replace(context.config, overlap_per_file=1)
    assert not can_start(context, 'main', 2)


def test_recorded_target_branch_drives_subsequent_scheduler_measurement(context):
    reserve(context, 1, ['src/a.py', 'src/b.py'])
    path = edit(context, 1, {'src/a.py': 'new\n'}, commit=True)
    context.git.at(path)._call(['update-ref', 'refs/remotes/origin/batch', 'HEAD'])
    edit(context, 1, {'src/b.py': 'new\n'}, commit=True)
    run = measure(context, context.store.get_run(1), parse_body(context.store.load_approval(1).body), 'batch')
    assert run.effective_files == ['src/b.py']
    assert claims(context)[0][0].run.effective_files == ['src/b.py']

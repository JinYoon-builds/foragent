"""Late claims wait within the credited runner boundary without blocking holders."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from foragent.issue import parse_body
from foragent.run_overlap import OVERLAP_POLL_SECONDS, wait_for_overlap
from foragent.run_waits import _remaining
from test_overlap_files import context, edit, reserve  # noqa: F401


def attempt(context, number):
    return SimpleNamespace(ctx=context, n=number, target_branch='main',
                           parsed=parse_body(context.store.load_approval(number).body))


def clock(context):
    now = [context.now()]
    context.now = lambda: now[0]
    context.monotonic = lambda: now[0].timestamp()
    context.store._now = context.now
    return now


@pytest.mark.parametrize('limit', [1, 2])
def test_late_cap_wait_credits_time_and_advertises_liveness(context, limit):
    context.config = replace(context.config, overlap_per_file=limit)
    now = clock(context)
    reserve(context, 1, ['own.py'])
    for number in range(2, limit + 2):
        reserve(context, number, ['shared.py'], lane=f'peer{number}')
    edit(context, 1, {'own.py': 'own\n'}, commit=True)
    edit(context, 1, {'shared.py': 'late\n'})
    before = _remaining(1, context)
    waits = []

    def sleep(seconds):
        run = context.store.get_run(1)
        assert run.waiting_until is not None
        assert not context.store.get_run(2).waiting_until
        waits.append(seconds)
        now[0] += timedelta(seconds=7)
        context.store.finish_run(2, 'merged')

    context.sleep = sleep
    wait_for_overlap(attempt(context, 1))
    run = context.store.get_run(1)
    assert waits == [OVERLAP_POLL_SECONDS]
    assert run.waiting_until is None and run.waited_seconds == 7
    assert _remaining(1, context) == before


def test_two_late_additions_give_the_earlier_starter_the_place(context):
    context.config = replace(context.config, overlap_per_file=1)
    now = clock(context)
    reserve(context, 1, ['first.py'])
    reserve(context, 2, ['second.py'], start=now[0] + timedelta(seconds=1))
    edit(context, 1, {'first.py': 'first\n', 'shared.py': 'one\n'}, commit=True)
    edit(context, 2, {'second.py': 'second\n', 'shared.py': 'two\n'}, commit=True)
    waits = []
    context.sleep = lambda seconds: pytest.fail('The earlier starter must keep running')
    wait_for_overlap(attempt(context, 1))

    def sleep(seconds):
        assert context.store.get_run(1).waiting_until is None
        waits.append(seconds)
        now[0] += timedelta(seconds=1)
        context.store.finish_run(1, 'merged')

    context.sleep = sleep
    wait_for_overlap(attempt(context, 2))
    assert waits == [OVERLAP_POLL_SECONDS]


@pytest.mark.parametrize('limit', [2, 1], ids=['default-cap', 'cap-one'])
@pytest.mark.parametrize('exclusive', [False, True], ids=['capacity', 'exclusive'])
@pytest.mark.parametrize('tied', [False, True], ids=['start-time', 'issue-tiebreak'])
def test_crossed_late_claims_let_the_earlier_run_proceed(context, limit, exclusive, tied):
    context.config = replace(context.config, overlap_per_file=limit)
    now = clock(context)
    earlier, later = (1, 2) if tied else (2, 1)
    reserve(context, earlier, ['a.py'], exclusive=['a.py'] if exclusive else [])
    reserve(context, later, ['b.py'], exclusive=['b.py'] if exclusive else [],
            start=now[0] + timedelta(seconds=0 if tied else 1))
    if limit == 2 and not exclusive:
        # Before expansion each file already has two admitted holders.
        reserve(context, 3, ['a.py', 'b.py'])
    edit(context, earlier, {'a.py': 'own\n'}, commit=True)
    edit(context, later, {'b.py': 'own\n'}, commit=True)
    edit(context, earlier, {'b.py': 'late\n'})
    edit(context, later, {'a.py': 'late\n'})
    context.sleep = lambda seconds: pytest.fail('Crossed claims must not block the earlier starter')
    wait_for_overlap(attempt(context, earlier))
    waits = []

    def sleep(seconds):
        assert context.store.get_run(later).waiting_until is not None
        # Even while the later run waits with both claims visible, the earlier
        # one can continue and release its claims, breaking the crossed wait.
        wait_for_overlap(attempt(context, earlier))
        waits.append(seconds)
        now[0] += timedelta(seconds=1)
        context.store.finish_run(earlier, 'merged')

    context.sleep = sleep
    wait_for_overlap(attempt(context, later))
    assert waits == [OVERLAP_POLL_SECONDS]
    assert context.store.get_run(later).waiting_until is None


@pytest.mark.parametrize('declared', [['own.py', 'shared.py'], ['*.py']], ids=['literal', 'glob'])
@pytest.mark.parametrize('exclusive', [False, True], ids=['capacity', 'exclusive'])
def test_declared_path_readded_after_narrowing_is_checked(context, declared, exclusive):
    from foragent.overlap_files import claims

    now = clock(context)
    reserve(context, 1, declared)
    edit(context, 1, {'own.py': 'own\n'}, commit=True)
    assert claims(context)[0][0].run.effective_files == ['own.py']
    for number in range(2, 3 if exclusive else 4):
        reserve(context, number, ['shared.py'], exclusive=['shared.py'] if exclusive else [])
    edit(context, 1, {'shared.py': 'readded\n'})
    # Scheduler observations must not turn a late addition into a held claim.
    claims(context)
    claims(context)
    waits = []

    def sleep(seconds):
        assert context.store.get_run(1).waiting_until is not None
        waits.append(seconds)
        now[0] += timedelta(seconds=1)
        context.store.finish_run(2, 'merged')

    context.sleep = sleep
    wait_for_overlap(attempt(context, 1))
    assert waits == [OVERLAP_POLL_SECONDS]
    assert context.store.get_run(1).effective_files == ['own.py', 'shared.py']


def test_concurrent_peer_observers_preserve_existing_conflict_evidence(context):
    from concurrent.futures import ThreadPoolExecutor
    from foragent.overlap_files import claims
    from foragent.run_overlap import _record_peers

    for number in (1, 2, 3):
        reserve(context, number, ['shared.py'])
    edit(context, 1, {'shared.py': 'own\n'}, commit=True)
    snapshots, _ = claims(context)
    owner, *peers = sorted(snapshots, key=lambda claim: claim.run.number)
    # The observers started before resolution completed and hold stale snapshots.
    context.store.update_run(1, overlap_records=[{'file': 'shared.py', 'issues': [],
                             'conflicted': True, 'resolution_seconds': 13, 'redo_count': 1}])
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda peer: _record_peers(context, owner, [peer]), peers))
    assert context.store.get_run(1).overlap_records == [
        {'file': 'shared.py', 'issues': [2, 3], 'conflicted': True, 'resolution_seconds': 13, 'redo_count': 1}]


def test_missing_approval_keeps_the_own_lane_rule_at_a_runner_boundary(context):
    reserve(context, 1, ['own.py'])
    reserve(context, 2, ['shared.py'], exclusive=['shared.py'], lane='api')
    edit(context, 1, {'own.py': 'own\n', 'shared.py': 'late\n'}, commit=True)
    owner = attempt(context, 1)
    context.store.clear_approval(1)
    context.sleep = lambda seconds: pytest.fail('Unknown Files must not count across lanes')
    wait_for_overlap(owner)


@pytest.mark.parametrize('previous_phase', [None, 'implementing', 'merging'])
@pytest.mark.parametrize('interrupted', [False, True], ids=['released', 'interrupted'])
def test_late_wait_logs_blockers_once_and_restores_phase_at_wait_end(context, previous_phase, interrupted):
    now = clock(context)
    context.config = replace(context.config, overlap_per_file=1, language='en')
    reserve(context, 1, ['own.py'])
    reserve(context, 2, ['a.py', 'b.py'])
    edit(context, 1, {'own.py': 'own\n'}, commit=True)
    edit(context, 1, {'a.py': 'late\n', 'b.py': 'late\n'})
    context.store.update_run(1, phase=previous_phase,
                             phase_started_at=(now[0] - timedelta(minutes=5)).isoformat())
    started, sleeps = now[0], []

    def sleep(seconds):
        run = context.store.get_run(1)
        assert run.phase == 'overlap-wait' and run.phase_started_at == started.isoformat()
        assert run.waiting_until is not None
        lines = context.store.log_path(1).read_text().splitlines()
        assert len(lines) == 1 and 'a.py: #2' in lines[0] and 'b.py: #2' in lines[0]
        sleeps.append(seconds)
        now[0] += timedelta(seconds=3)
        if interrupted:
            raise RuntimeError('Wait interrupted')
        if len(sleeps) == 2:
            context.store.finish_run(2, 'merged')

    context.sleep = sleep
    if interrupted:
        with pytest.raises(RuntimeError, match='Wait interrupted'):
            wait_for_overlap(attempt(context, 1))
    else:
        wait_for_overlap(attempt(context, 1))
    run = context.store.get_run(1)
    assert run.phase == previous_phase and run.phase_started_at == now[0].isoformat()
    assert run.waited_seconds == (now[0] - started).total_seconds()
    assert run.waiting_until is None
    assert len(context.store.log_path(1).read_text().splitlines()) == 1


@pytest.mark.parametrize('exclusive', [False, True])
def test_batch_late_union_uses_configured_base_and_preserves_exclusive(context, git_history, exclusive):
    from conftest import git_command

    now = clock(context)
    git_history.update({'.gitattributes': '*.md merge=union\n'})
    context.git.fetch('origin', 'main')
    git_command(context.repo_root, 'update-ref', 'refs/remotes/origin/trunk', git_history.tip)
    context.config = replace(context.config, base_branch='trunk', overlap_per_file=1)
    reserve(context, 1, ['own.py'])
    reserve(context, 2, ['*.md'], exclusive=['shared*.md'] if exclusive else [])
    edit(context, 1, {'own.py': 'own\n'}, commit=True)
    path = edit(context, 1, {'shared[1].md': 'late\n'})
    # Batch attributes disagree with the configured base, as do local edits.
    git_history.update({'.gitattributes': '*.md merge=text\n'})
    context.git.fetch('origin', 'main')
    git_command(context.repo_root, 'update-ref', 'refs/remotes/origin/batch', git_history.tip)
    (path / '.gitattributes').write_text('*.md merge=text\n')
    calls, call = [], context.git._call

    def observe(args):
        if args[:1] == ['check-attr']:
            calls.append(args)
        return call(args)

    context.git._call = observe
    waits = []

    def sleep(seconds):
        assert exclusive, 'Union capacity must not cause a late wait'
        assert 'shared[1].md: #2' in context.store.log_path(1).read_text()
        waits.append(seconds)
        now[0] += timedelta(seconds=1)
        context.store.finish_run(2, 'merged')

    context.sleep = sleep
    owner = attempt(context, 1)
    owner.target_branch = 'batch'
    wait_for_overlap(owner)
    assert waits == ([OVERLAP_POLL_SECONDS] if exclusive else [])
    assert len(calls) == (3 if exclusive else 1), 'Read one attribute batch per wait check'
    assert all('--source=origin/trunk' in args for args in calls)
    assert context.store.get_run(1).overlap_records == []


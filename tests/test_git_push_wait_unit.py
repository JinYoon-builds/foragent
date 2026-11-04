"""Push retry facts retain a head's budget and consume each lease once."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

from foragent import orch_github
from foragent.git import GitError, push_server_error
from foragent.state import StateStore
from foragent.tracker import TrackerError

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_skills_autosync import isolated_home  # noqa: E402,F401


@pytest.mark.parametrize(('diagnostic', 'expected'), [
    ('remote: Internal Server Error', True),
    (' ! [remote rejected] HEAD -> branch (Internal Server Error)', True),
    ('RPC failed; HTTP 502 curl 22', True),
    ('The requested URL returned error: 503', True),
    (' ! [rejected] HEAD -> branch (stale info)', False),
    ('remote: GH013 Repository rule violations', False),
    ('The requested URL returned error: 403', False),
])
def test_push_server_fault_diagnostics(diagnostic, expected):
    assert push_server_error(diagnostic) is expected


@pytest.fixture
def pushing(tmp_path):
    clock = [datetime(2026, 11, 4, tzinfo=UTC)]
    store = StateStore(tmp_path, now=lambda: clock[0])
    store.save_approval(number=1, body='approved', body_sha='body', acceptance_blobs={},
                        approved_by='human', approved_at=clock[0].isoformat())
    ctx = SimpleNamespace(store=store, now=lambda: clock[0],
                          config=SimpleNamespace(transient_max_wait_minutes=10), repo='owner/repo')
    return ctx, clock


def rejected(message='remote: Internal Server Error'):
    raise GitError(message)


def attempt(ctx, head='verified', do_push=rejected, *, kind='refresh', lease='old'):
    orch_github.wait_out_push(ctx, 1, do_push, head=head, lease=lease, branch='branch', kind=kind)


def test_budget_survives_retries_but_resets_after_success_or_a_different_head(pushing):
    ctx, clock = pushing
    with pytest.raises(TrackerError):
        attempt(ctx)
    began = orch_github.pending_push(ctx, 1)['started']
    clock[0] += timedelta(minutes=9)
    with pytest.raises(TrackerError):
        attempt(ctx)
    assert orch_github.pending_push(ctx, 1)['started'] == began
    clock[0] += timedelta(minutes=1)
    with pytest.raises(GitError, match='Internal Server Error'):
        attempt(ctx)
    assert orch_github.pending_push(ctx, 1) is None
    with pytest.raises(TrackerError):
        attempt(ctx)
    attempt(ctx, do_push=lambda: None)
    assert orch_github.pending_push(ctx, 1) is None
    clock[0] += timedelta(minutes=11)
    with pytest.raises(TrackerError):
        attempt(ctx, head='new-head')
    assert orch_github.pending_push(ctx, 1)['started'] == clock[0].isoformat()


@pytest.mark.parametrize(('kind', 'lease'), [('refresh', 'old'), ('publish', 'old'), ('publish', '')])
def test_resume_uses_exact_lease_once_and_refusal_consumes_it_without_waiting(pushing, kind, lease):
    ctx, clock = pushing
    with pytest.raises(TrackerError):
        attempt(ctx, kind=kind, lease=lease)
    calls = []
    git = SimpleNamespace(head_sha=lambda: 'verified', _call=lambda args: '',
                          push_head=lambda branch, **kw: calls.append(('head', branch, kw)),
                          push=lambda branch, **kw: calls.append(('branch', branch, kw)))
    assert orch_github.resume_push(ctx, 1, git) is (kind == 'refresh')
    assert calls == [('head' if kind == 'refresh' else 'branch', 'branch', {'lease': lease})]
    assert orch_github.resume_push(ctx, 1, git) is None
    assert len(calls) == 1
    with pytest.raises(TrackerError):
        attempt(ctx, kind=kind, lease=lease)
    began = clock[0]
    with pytest.raises(GitError, match='403'):
        attempt(ctx, do_push=lambda: rejected('returned error: 403'), kind=kind, lease=lease)
    assert clock[0] == began
    assert orch_github.pending_push(ctx, 1) is None


def test_wait_before_review_never_claims_a_pass(pushing):
    ctx, _ = pushing
    with pytest.raises(TrackerError):
        orch_github.wait_out_push(ctx, 1, rejected, head='unreviewed', lease='', branch='branch', kind='publish')
    calls = []
    git = SimpleNamespace(head_sha=lambda: 'unreviewed', _call=lambda args: '',
                          push=lambda branch, **kw: calls.append((branch, kw)),
                          push_head=lambda *a, **kw: pytest.fail('Publish must resume its branch push'))
    assert orch_github.resume_push(ctx, 1, git) is False
    assert calls == [('branch', {'lease': ''})]


def test_different_head_starts_its_own_budget_while_an_older_push_is_waiting(pushing):
    ctx, clock = pushing
    with pytest.raises(TrackerError):
        attempt(ctx)
    clock[0] += timedelta(minutes=11)
    with pytest.raises(TrackerError):
        attempt(ctx, head='other')
    assert orch_github.pending_push(ctx, 1)['started'] == clock[0].isoformat()


def test_refresh_can_resume_when_only_the_remote_branch_existed(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from test_git_push_outage import INTERNAL, Remote, start
    from test_github_drop import outage_case, assert_wait, restart_at_wait_end, assert_merged_once

    case = outage_case(git_history, tmp_path, monkeypatch, 'review')
    initial = start(case, 'review')
    remote = Remote(case, [INTERNAL])
    assert initial(1, case.ctx) == 'limit-wait'
    case.clock[0] = assert_wait(case, 'Internal Server Error')
    git_command(case.root, 'update-ref', '-d', 'refs/heads/foragent/issue-1')
    implementations, reviews = len(case.implementer.calls), len(case.reviewer.calls)
    assert restart_at_wait_end(case) == 'merged'
    assert_merged_once(case)
    assert len(case.implementer.calls) == implementations
    assert len(case.reviewer.calls) == reviews
    assert remote.landed and set(remote.landed) == {remote.tried[0]}


def test_merge_owner_keeps_its_existing_tick_retry(pushing):
    ctx, clock = pushing
    ctx.merge_owner_started_at = clock[0]
    with pytest.raises(GitError, match='Internal Server Error'):
        attempt(ctx)
    assert orch_github.pending_push(ctx, 1) is None
    assert not (ctx.store.root / 'orch-events.jsonl').exists()


def test_first_publish_lost_lease_holds_without_a_cleanup_overwrite(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from test_git_push_outage import INTERNAL, BRANCH, Remote, start
    from test_github_drop import outage_case, assert_wait, restart_at_wait_end

    case = outage_case(git_history, tmp_path, monkeypatch, 'run')
    initial = start(case, 'run')
    other = case.history.tip
    remote = Remote(case, [INTERNAL], on_failure=lambda: git_command(
        case.history.origin, 'update-ref', f'refs/heads/{BRANCH}', other))
    assert initial(1, case.ctx) == 'limit-wait'
    case.clock[0] = assert_wait(case, 'Internal Server Error')
    assert restart_at_wait_end(case) == 'held'
    assert remote.head() == other and remote.landed == []
    assert len(remote.tried) == 2
    assert case.tracker.merged_prs == []
    assert case.store.load_flag(1) is not None


def test_pre_review_push_resumes_checks_and_review_without_repeating_implementation(git_history, tmp_path, monkeypatch):
    from dataclasses import replace
    from test_git_push_outage import INTERNAL, Remote, start, drive
    from test_github_drop import outage_case, assert_merged_once

    case = outage_case(git_history, tmp_path, monkeypatch, 'run')
    initial = start(case, 'run')
    case.ctx.config = replace(case.ctx.config, review_scope='pr-body')
    remote = Remote(case, [INTERNAL])
    assert drive(case, initial, 'Internal Server Error')[-1] == 'merged'
    assert_merged_once(case)
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1
    assert remote.landed and set(remote.landed) == {remote.tried[0]}

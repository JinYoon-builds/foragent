"""Runs, recovery and batches keep to the person boundary after ownership changes (#1004)."""

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent.config import Config  # noqa: E402
from foragent.tracker import FakeTracker, Issue  # noqa: E402


def owned_case(git_history, tmp_path, monkeypatch):
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, ["by:alice"])
    return case


def relabel(tracker, owner):
    tracker.remove_labels(1, [label for label in tracker.get_issue(1).labels if label.startswith("by:")])
    tracker.add_labels(1, [f"by:{owner}"])


def test_an_owned_runner_failure_still_blocks_with_its_reason(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = owned_case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [lambda kwargs: (_ for _ in ()).throw(RuntimeError("executor crashed"))]
    assert run_issue(1, case.ctx) == "error"
    assert "blocked" in case.tracker.get_issue(1).labels


def test_stop_refused_on_another_persons_issue_is_recorded_locally_only(monkeypatch):
    from datetime import UTC, datetime
    from foragent import scheduler_stop
    from foragent.reasons import Reason

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's", "", labels=["by:bob", "running"])
    flags, events = {}, []
    store = SimpleNamespace(
        load_flag=lambda n: flags.get(n), local_issues=lambda: [],
        save_flag=lambda n, reason, flagged_at: flags.__setitem__(n, SimpleNamespace(reason=reason, flagged_at=flagged_at)))
    monkeypatch.setattr(scheduler_stop.scheduler, "append_event", lambda *args: events.append(args))
    now = datetime(2026, 10, 31, tzinfo=UTC)
    ctx = SimpleNamespace(tracker=tracker, store=store, config=Config(legacy_owner="alice"), now=lambda: now)
    run = SimpleNamespace(kind="issue", number=1, started_at=now.isoformat(), lane="main")
    report = SimpleNamespace(actions=[])
    scheduler_stop._flag_stop_refused(ctx, run, report, PermissionError("EPERM"))
    scheduler_stop._flag_stop_refused(ctx, run, report, PermissionError("EPERM"))
    assert tracker.list_comments(1) == [] and "blocked" not in tracker.get_issue(1).labels
    assert flags[1].reason == Reason.STOP_REFUSED and len(events) == 1


def test_batch_reconcile_leaves_another_persons_children_open(monkeypatch):
    from foragent import scheduler, scheduler_checks

    tracker = FakeTracker(login="alice")
    tracker.add_issue(9, "Bob's order", "", labels=["by:bob"])
    tracker.add_issue(2, "Bob's child", "", labels=["by:bob"])
    tracker.add_issue(5, "Alice's order", "", labels=["by:alice"])
    tracker.add_issue(6, "Alice's child", "", labels=["by:alice"])
    parents = {2: 9, 6: 5}
    monkeypatch.setattr(tracker, "parent_of", lambda n: parents.get(n))
    rows = [{"number": n, "title": "", "body": "", "labels": sorted(tracker.get_issue(n).labels),
             "state": "open", "merged": False} for n in (9, 2, 5, 6)]
    for child, parent in parents.items():
        rows.append({"number": 100 + child, "title": "", "body": "", "labels": [], "state": "open",
                     "merged": True, "pr_number": 100 + child, "pr_head": f"foragent/issue-{child}",
                     "pr_base": f"foragent/batch-{parent}"})
    store = SimpleNamespace(local_issues=lambda: rows)
    monkeypatch.setattr(scheduler, "local_issue_models", lambda store: [
        Issue(row["number"], "", "", frozenset(row["labels"]), "open") for row in rows if not row["merged"]])
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *a, **kw: None)
    finalized = []
    monkeypatch.setattr(scheduler, "_batch_pr", lambda parent, ctx: finalized.append(parent))
    ctx = SimpleNamespace(tracker=tracker, store=store, config=Config(), now=lambda: None)
    scheduler_checks._reconcile_batches(ctx, SimpleNamespace(actions=[]))
    assert tracker.get_issue(2).state == "open", "Bob's factory closes his batch child"
    assert tracker.get_issue(6).state == "closed"


def test_batch_finalization_skips_another_persons_parent(monkeypatch):
    from foragent import run_publish_merge

    tracker = FakeTracker(login="alice")
    tracker.add_issue(9, "Bob's order", "", labels=["by:bob"])
    monkeypatch.setattr(tracker, "sub_issues", lambda n: [Issue(2, "", "", frozenset(), "closed")])
    fetched = []
    git = SimpleNamespace(fetch=lambda *a: fetched.append(a))
    ctx = SimpleNamespace(tracker=tracker, git=git, store=SimpleNamespace(local_issues=lambda: []),
                          config=Config(legacy_owner="alice"))
    run_publish_merge._batch_pr(9, ctx)
    assert fetched == [] and tracker.created_prs == []


def test_a_cancelled_blocker_question_names_the_orderer(git_history, tmp_path, monkeypatch):
    from foragent import scheduler
    from test_orch_review_handoff import tick_context

    case = owned_case(git_history, tmp_path, monkeypatch)
    blocker = Issue(7, "Gone", "", frozenset(), "closed", state_reason="not_planned")
    monkeypatch.setattr(scheduler, "_blockers", lambda ctx, n, confirmed=False: [blocker])
    issue = case.tracker.get_issue(1)
    approval = case.store.load_approval(1)
    report = scheduler.TickReport()
    assert scheduler._check_prerequisites(tick_context(case), issue, approval, report) is False
    assert any(c.body.count("@alice") and "#7" in c.body for c in case.tracker.list_comments(1))


def test_no_push_after_the_owner_changed(monkeypatch):
    from foragent import run_publish_pr
    from foragent.run_attempt import OwnershipLost

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's now", "", labels=["by:bob"])
    pushed = []
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    attempt = SimpleNamespace(ctx=ctx, n=1, rebuilt=False, branch="foragent/issue-1",
                              git=SimpleNamespace(push=lambda *a, **kw: pushed.append(a), head_sha=lambda: "x"))
    try:
        run_publish_pr._push_branch(attempt)
    except OwnershipLost:
        pass
    assert pushed == []


def test_recovery_keeps_the_work_when_the_owner_read_is_malformed(tmp_path, monkeypatch):
    from foragent import scheduler, scheduler_recovery

    class Malformed(FakeTracker):
        def get_issue(self, n):
            raise ValueError("Expecting property name enclosed in double quotes")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "unfinished.py").write_text("work\n")
    preserved, removed = [], []
    monkeypatch.setattr(scheduler, "_preserve_failed_implementation",
                        lambda n, ctx, git, why, **kw: preserved.append(kw["tracker_available"]))
    store = SimpleNamespace(local_issues=lambda: [], load_approval=lambda n: None,
                            github_wait_until=lambda now: None)
    git = SimpleNamespace(at=lambda path: path, remove_worktree=lambda path: removed.append(path))
    ctx = SimpleNamespace(tracker=Malformed(login="alice"), config=Config(legacy_owner="alice"), store=store,
                          git=git, now=lambda: None)
    scheduler_recovery._remove_run_worktree(ctx, 1, worktree, SimpleNamespace(actions=[]), outcome="killed")
    assert preserved == [False], "The work is kept locally before removal, without pushing it"
    assert removed == [worktree]


def test_a_start_check_never_writes_to_an_issue_that_changed_hands(monkeypatch):
    from foragent import orch_scope, scheduler, scheduler_start

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's now", "edited body", labels=["by:bob", "approved", "ready"])
    checked = []
    monkeypatch.setattr(scheduler, "_check_integrity", lambda *a, **kw: checked.append("integrity") or False)
    monkeypatch.setattr(scheduler, "_can_start_in_lane", lambda *a, **kw: True)
    tracker.record_label(1, "approved", "human")
    approved_at = tracker.label_events(1)[-1].created_at
    config = Config(legacy_owner="alice", approvers=["human"])
    ctx = SimpleNamespace(tracker=tracker, config=config,
                          store=SimpleNamespace(load_approval=lambda n: SimpleNamespace(approved_at=approved_at)))
    ctx._scope = orch_scope.Scope(ctx)
    assert scheduler_start._start_checks(ctx, 1, "main", lambda issue, lane: True, "origin/main",
                                         SimpleNamespace(actions=[])) is None
    assert checked == [] and tracker.list_comments(1) == []


import pytest


@pytest.mark.parametrize('entry', ['run_issue', 'review_only'])
def test_foreign_run_entry_refuses_before_records_worktrees_or_executors(
    git_history, tmp_path, monkeypatch, capsys, entry,
):
    from foragent import runner

    case = owned_case(git_history, tmp_path, monkeypatch)
    relabel(case.tracker, 'bob')
    # Even a stale earlier context cannot authorize this new run's entry.
    before = {p.relative_to(case.store.root): p.read_bytes()
              for p in case.store.root.rglob('*') if p.is_file()}
    monkeypatch.setattr(case.git, 'remove_worktree', lambda *a: pytest.fail('Foreign entry removed a worktree'))
    assert getattr(runner, entry)(1, case.ctx) == 'refused'
    assert case.implementer.calls == [] and case.reviewer.calls == []
    assert {p.relative_to(case.store.root): p.read_bytes()
            for p in case.store.root.rglob('*') if p.is_file()} == before
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1 and 'bob' in output.err


def test_start_keeps_tick_snapshot_through_board_sync(git_history, tmp_path, monkeypatch):
    from datetime import timedelta
    from foragent import scheduler
    from team_v1_support import factory

    case = owned_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, 'blocked')
    case.tracker.remove_labels(1, ['running'])
    case.tracker.record_label(1, 'approved', 'human')
    ctx, starts = factory(case, tmp_path / 'alice', 'alice', monkeypatch)
    reads = []
    monkeypatch.setattr(ctx.tracker, 'authenticated_login', lambda: reads.append('login') or 'alice')

    def board_sync(ctx, number, state, **kwargs):
        if state == 'Running':
            relabel(case.tracker, 'bob')

    monkeypatch.setattr(scheduler.board, 'sync_status', board_sync)
    scheduler.tick(ctx)
    assert starts == [1] and reads == ['login']
    # The next operation sees Bob, even though Alice's first tick had been authorized.
    ctx.store.finish_run(1, 'blocked')
    case.clock[0] += timedelta(minutes=6)
    scheduler.tick(ctx)
    assert starts == [1] and reads == ['login', 'login']
    assert {row['number'] for row in ctx.store.local_issues()} == {1}


@pytest.mark.parametrize('entry', ['run_issue', 'review_only'])
def test_paused_entry_uses_cached_ownership_without_github_reads(git_history, tmp_path, monkeypatch, entry):
    from datetime import timedelta
    from foragent import runner

    case = owned_case(git_history, tmp_path, monkeypatch)
    case.store.record_issue(case.tracker.get_issue(1), case.config.lanes)
    if entry == 'review_only':
        case.store.finish_run(1, 'blocked')
    case.store.pause_github(case.clock[0] + timedelta(minutes=5))
    monkeypatch.setattr(case.tracker, 'get_issue', lambda n: pytest.fail('GitHub pause must stay offline'))
    assert getattr(runner, entry)(1, case.ctx) == 'limit-wait'
    assert case.implementer.calls == [] and case.reviewer.calls == []

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


def test_an_owner_edit_during_the_run_stops_before_any_push_pr_or_merge(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent.runner import run_issue

    case = owned_case(git_history, tmp_path, monkeypatch)

    def implement_then_lose(kwargs):
        relabel(case.tracker, "bob")
        return case.implement(kwargs)

    case.implementer.steps = [implement_then_lose]
    comments = list(case.tracker.list_comments(1))
    assert run_issue(1, case.ctx) == "refused"
    assert case.tracker.created_prs == [] and case.tracker.merged_prs == []
    assert case.tracker.list_comments(1) == comments
    assert "blocked" not in case.tracker.get_issue(1).labels
    remote = git_command(case.history.origin, "for-each-ref", "refs/heads/foragent/issue-1")
    assert remote == "", "The WIP must stay on the local branch, not be pushed for Bob's issue"
    assert case.git.local_branch_sha("foragent/issue-1") is not None
    assert case.store.get_run(1).outcome == "refused"


def test_a_runner_failure_after_an_owner_edit_writes_nothing_on_the_issue(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = owned_case(git_history, tmp_path, monkeypatch)

    def fail(kwargs):
        relabel(case.tracker, "bob")
        raise RuntimeError("executor crashed")

    case.implementer.steps = [fail]
    before = case.tracker.get_issue(1)
    comments = list(case.tracker.list_comments(1))
    assert run_issue(1, case.ctx) == "error"
    assert case.tracker.get_issue(1).labels == (before.labels | {"by:bob"}) - {"by:alice"}
    assert case.tracker.list_comments(1) == comments


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


def test_a_question_written_after_an_owner_edit_is_not_posted_on_the_new_owners_issue(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = owned_case(git_history, tmp_path, monkeypatch)

    def ask_after_handover(kwargs):
        relabel(case.tracker, "bob")
        question = kwargs["worktree"] / ".foragent/QUESTION.md"
        question.parent.mkdir(parents=True, exist_ok=True)
        question.write_text("Keep or change?\n")

    case.implementer.steps = [ask_after_handover]
    comments = list(case.tracker.list_comments(1))
    labels = case.tracker.get_issue(1).labels
    assert run_issue(1, case.ctx) == "refused"
    assert case.tracker.list_comments(1) == comments
    assert case.tracker.get_issue(1).labels == (labels | {"by:bob"}) - {"by:alice"}


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


def test_every_run_write_rechecks_the_owner():
    from foragent import orch_scope
    from foragent.run_attempt import OwnershipLost

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Mine", "", labels=["by:alice"])
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    guarded = orch_scope.RunTracker(ctx, 1)
    guarded.comment(1, "Before")
    relabel(tracker, "bob")
    for write in (lambda: guarded.comment(1, "After"), lambda: guarded.add_labels(1, ["blocked"]),
                  lambda: guarded.create_pr(head="foragent/issue-1", base="main", title="t", body="b")):
        try:
            write()
        except OwnershipLost:
            continue
        raise AssertionError("A run write landed on Bob's issue")
    assert [c.body for c in tracker.list_comments(1)] == ["Before"] and tracker.created_prs == []
    assert guarded.get_issue(1).number == 1  # Reads pass through.


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


def test_rerun_review_posts_no_audit_comment_after_the_owner_changed(git_history, tmp_path, monkeypatch):
    from foragent import orch

    case = owned_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])

    def handover(n, ctx, **kwargs):
        relabel(case.tracker, "bob")
        return "refused"

    monkeypatch.setattr(orch, "review_only", handover)
    comments = list(case.tracker.list_comments(1))
    orch.rerun_review(case.ctx, 1)
    assert case.tracker.list_comments(1) == comments


def test_a_child_run_does_not_open_a_batch_pr_for_a_parent_that_changed_hands():
    from foragent import orch_scope
    from foragent.tracker import WriteRefused

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Child", "", labels=["by:alice"])
    tracker.add_issue(9, "Parent, Bob's now", "", labels=["by:bob"])
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    guarded = orch_scope.RunTracker(ctx, 1)
    for write in (lambda: guarded.create_pr(head="foragent/batch-9", base="main", title="t", body="b"),
                  lambda: guarded.comment(9, "Batch PR ready")):
        try:
            write()
        except WriteRefused:
            continue
        raise AssertionError("The child run wrote to Bob's order")
    assert tracker.created_prs == [] and tracker.list_comments(9) == []
    guarded.comment(1, "Own child")  # The run itself goes on.
    assert [c.body for c in tracker.list_comments(1)] == ["Own child"]


def test_stop_rechecks_the_owner_after_waiting_for_the_tick_lock(monkeypatch):
    from contextlib import contextmanager
    from foragent import orch_stop, scheduler
    from foragent.orch import OrchError

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Mine", "", labels=["by:alice", "running"])
    killed = []
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **kw: killed.append(a) or True)

    @contextmanager
    def lock_after_handover(ctx, n):
        relabel(tracker, "bob")  # Bob took the issue while this stop waited.
        yield

    monkeypatch.setattr(orch_stop, "_tick_lock", lock_after_handover)
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    try:
        orch_stop.stop(ctx, 1, "Replan")
    except OrchError as exc:
        assert "bob" in str(exc)
    else:
        raise AssertionError("A stop of Bob's issue reported success")
    assert killed == []


def test_a_board_card_is_not_added_or_moved_after_the_issue_changed_hands_during_lookup():
    from foragent.board import Project

    def project_tracker(card_exists):
        answers = iter([False, True])  # Alice's at the first check, Bob's at the write.
        calls = []

        def graphql(query, variables):
            calls.append(query.split("(")[0].split()[-1])
            if "ForagentBoardFields" in query:
                return {"node": {"fields": {"nodes": [{"id": "F", "name": "Status", "options": [
                    {"id": "o1", "name": "Done"}, {"id": "o2", "name": "Ready"}]}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            if "ForagentBoardIssue" in query:
                nodes = [{"id": "I", "project": {"id": "P"},
                          "fieldValueByName": {"name": "Ready", "optionId": "o2"}}] if card_exists else []
                return {"repository": {"issue": {"id": "C", "projectItems": {
                    "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            return {}

        tracker = SimpleNamespace(graphql=graphql, board_write_refused=lambda n: next(answers))
        return tracker, calls

    for card_exists in (False, True):
        tracker, calls = project_tracker(card_exists)
        Project(tracker, "owner/repo", "P").set_values(1, {"Status": "Done"})
        assert not [name for name in calls if name.startswith("ForagentAdd") or name.startswith("ForagentSet")], calls


def test_a_start_whose_labels_were_refused_launches_nothing(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case
    from team_v1_support import factory, legacy

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    legacy(case.ctx.config)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["by:alice"])
    case.tracker.record_label(1, "approved", "human")
    alice, starts = factory(case, tmp_path / "alice", "alice", monkeypatch)
    remove = alice.tracker.remove_labels

    def remove_then_handover(n, labels):
        result = remove(n, labels)
        if "ready" in labels:
            relabel(alice.tracker, "bob")  # Bob takes it between the two start labels.
        return result

    monkeypatch.setattr(alice.tracker, "remove_labels", remove_then_handover)
    tick(alice)
    assert starts == []
    run = alice.store.get_run(1)
    assert run is None or run.status != "running"
    assert "running" not in case.tracker.get_issue(1).labels

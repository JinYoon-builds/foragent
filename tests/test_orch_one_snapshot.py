"""One ownership snapshot per tick or command, and no writes without one (#1004)."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import orch_scope, runner, scheduler, scheduler_stop  # noqa: E402
from foragent.config import Config  # noqa: E402
from foragent.run_attempt import OwnershipLost  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError  # noqa: E402
import test_orch_review_handoff as handoff  # noqa: E402
from test_orch_premerge import merge_case  # noqa: E402, F401
from runner_support import run_case  # noqa: E402, F401
import test_stop_whole_tree as stop_fakes  # noqa: E402
from test_keep_unfinished_work import _assert_saved  # noqa: E402
from test_orch_stop import _case as stop_case  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=UTC)


@pytest.fixture
def fake_processes(monkeypatch, tmp_path, no_forbidden_git_commands):
    return stop_fakes.fake_processes.__wrapped__(monkeypatch, tmp_path, no_forbidden_git_commands)


def queue_case(tmp_path, *issues, legacy="alice"):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(legacy_owner=legacy))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, NOW)
    return store, tracker, recorder, context


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def relabel(tracker, number, old, new):
    tracker.remove_labels(number, [old])
    tracker.add_labels(number, [new])


def team_case(git_history, tmp_path, monkeypatch, cached, current):
    """Issue #1 cached as one owner while the tracker already shows another."""
    case = handoff.make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, [f"by:{cached}"])
    case.ctx.tracker.get_issue(1)
    relabel(case.tracker, 1, f"by:{cached}", f"by:{current}")
    return case


@pytest.mark.parametrize("cached, current, restored", [("bob", "alice", True), ("alice", "bob", False)])
def test_stop_inner_paths_use_the_commands_entry_snapshot(
    git_history, tmp_path, monkeypatch, fake_processes, cached, current, restored,
):
    case, tree, main = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, [f"by:{cached}"])
    case.ctx.tracker.get_issue(1)
    relabel(case.tracker, 1, f"by:{cached}", f"by:{current}")
    code = main(["orch", "stop", "1", "--keep-work", "--why", "Keep the work"])
    labels = case.tracker.get_issue(1).labels
    if restored:
        assert code == 0 and not {row.pid for row in tree} & fake_processes.live
        assert "running" not in labels and "ready" in labels
        _assert_saved(case, "Keep the work")
    else:
        assert code == 1 and {row.pid for row in tree} <= fake_processes.live and "running" in labels


def test_a_tick_writes_nothing_to_a_team_issue_before_it_reads(git_history, tmp_path, monkeypatch):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    run = SimpleNamespace(kind="issue", number=1, started_at=NOW.isoformat(), lane="main")
    seen = []

    def reap(ctx, report):
        seen.append(True)
        scheduler_stop._flag_stop_refused(ctx, run, report, PermissionError("EPERM"))

    monkeypatch.setattr(scheduler, "_reap_inactive_runs", reap)
    before = len(case.tracker.list_comments(1))
    scheduler.tick(handoff.tick_context(case))
    assert seen and len(case.tracker.list_comments(1)) == before
    assert "blocked" not in case.tracker.get_issue(1).labels


def test_a_failed_refresh_leaves_no_snapshot_to_write_from(git_history, tmp_path, monkeypatch):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    ctx = handoff.tick_context(case)
    seen = []
    monkeypatch.setattr(case.tracker, "list_issues",
                        lambda state="all": (_ for _ in ()).throw(TrackerError("HTTP 502")))
    monkeypatch.setattr(scheduler, "_recover_runs",
                        lambda ctx, report: seen.append(orch_scope.may_write(ctx, 1)))
    scheduler.tick(ctx)
    assert seen == [False]


@pytest.mark.parametrize("effect", ["HTTP 502", "exit"])
def test_a_failed_run_entry_read_writes_nothing(git_history, tmp_path, monkeypatch, effect):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    before = dict(comments=len(case.tracker.list_comments(1)), labels=case.tracker.get_issue(1).labels)

    def fails(_):
        if effect == "exit":
            raise SystemExit("stopped during the entry read")
        raise TrackerError(effect)

    monkeypatch.setattr(orch_scope, "observe", lambda tracker: SimpleNamespace(get_issue=fails))
    if effect == "exit":
        with pytest.raises(SystemExit):
            runner.run_issue(1, case.ctx)
    else:
        assert runner.run_issue(1, case.ctx) == "error"
    assert len(case.tracker.list_comments(1)) == before["comments"]
    assert case.tracker.get_issue(1).labels == before["labels"]


def test_foreign_run_issue_prints_one_line(git_history, tmp_path, monkeypatch, capsys):
    case = team_case(git_history, tmp_path, monkeypatch, "bob", "bob")
    assert runner.run_issue(1, case.ctx) == "refused"
    captured = capsys.readouterr()
    assert len(captured.err.splitlines()) == 1 and "bob" in captured.err and captured.out == ""


def test_ownership_lost_during_a_run_prints_its_one_line(git_history, tmp_path, monkeypatch, capsys):
    case = handoff.make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    monkeypatch.setattr(runner.capacity, "hold_workload", lambda *a, **kw: nullcontext())  # A busy machine prints a wait line.
    monkeypatch.setattr(runner, "_execute",
                        lambda n, ctx, worktree: (_ for _ in ()).throw(OwnershipLost("#1 is Bob's now")))
    assert runner.run_issue(1, case.ctx) == "refused"
    assert capsys.readouterr().err.splitlines() == ["#1 is Bob's now"]


@pytest.mark.parametrize("action, payload", [
    ("comment", {"body": "Mine once"}), ("add_labels", {"labels": ["ready"]}), ("close_issue", {"body": None})])
def test_an_observed_owner_edit_refuses_a_queued_write_inspectably(tmp_path, action, payload):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(1, action, lanes={}, **payload)
    store.queue_github_write(2, "comment", body="Still mine")
    relabel(tracker, 1, "by:alice", "by:bob")
    recorder.get_issue(1)
    [refused] = store.refused_github_writes()
    assert refused["issue"] == 1 and refused["action"] == action and "by:bob" in refused["refused"]
    assert [entry["issue"] for entry in store.github_outbox()] == [2]
    assert [(e["kind"], e["issue"], e["action"], e["owner"]) for e in events(store)] == [
        ("ownership-refused", 1, action, "bob")]
    recorder.flush_outbox()
    assert tracker.list_comments(1) == [] and tracker.get_issue(1).state == "open"
    assert [c.body for c in tracker.list_comments(2)] == ["Still mine"]


def test_a_persons_other_label_edit_still_drops_the_write(tmp_path):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "add_labels", lanes={}, labels=["ready"])
    tracker.add_labels(1, ["blocked"])
    recorder.get_issue(1)
    assert store.github_outbox() == [] and store.refused_github_writes() == []


def batch_case(git_history, tmp_path, monkeypatch, parent_owner):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "alice")
    object.__setattr__(case.ctx.config, "review", "batch")
    case.tracker.add_issue(99, "Parent", "", labels=[f"by:{parent_owner}"])
    case.ctx.tracker.get_issue(99)
    case.tracker.set_parent(1, 99)
    return case


@pytest.mark.parametrize("owner, expected", [("bob", None), ("alice", 99)])
def test_a_batch_parent_must_be_this_persons(git_history, tmp_path, monkeypatch, owner, expected):
    case = batch_case(git_history, tmp_path, monkeypatch, owner)
    assert orch_scope.batch_parent(case.ctx, 1) == expected


def test_a_child_of_another_persons_parent_never_touches_their_batch_branch(git_history, tmp_path, monkeypatch):
    case = batch_case(git_history, tmp_path, monkeypatch, "bob")
    case.ctx._scope = orch_scope.Scope(case.ctx)
    branches = []
    monkeypatch.setattr(case.ctx.git, "ensure_batch_branch", lambda branch, base: branches.append(branch))
    monkeypatch.setattr(runner, "_implement_and_publish", lambda n, ctx, git, target_branch: target_branch)
    monkeypatch.setattr(runner, "_restore_approved_base", lambda *a, **kw: True)
    assert runner._execute(1, case.ctx, case.store.worktree_path(1)) == case.ctx.config.base_branch
    assert branches == []


def test_foreign_run_issue_takes_no_machine_slot(git_history, tmp_path, monkeypatch):
    case = team_case(git_history, tmp_path, monkeypatch, "bob", "bob")
    held = []
    monkeypatch.setattr(runner.capacity, "hold_workload", lambda *a, **kw: held.append(kw) or (_ for _ in ()).throw(AssertionError))
    assert runner.run_issue(1, case.ctx) == "refused"
    assert held == []


def test_a_failed_read_uses_no_cached_row_but_the_owner_it_names_still_refuses(tmp_path):
    store, tracker, recorder, context = queue_case(tmp_path, (1, ["by:bob"]), (2, []), legacy=None)
    context._scope = orch_scope.no_snapshot(context)
    assert orch_scope.may_write(context, 1) is False
    assert orch_scope.may_write(context, 2) is True  # The locked singleton path.


def test_no_snapshot_ignores_a_callers_cached_row_on_a_team_repo(tmp_path):
    store, tracker, recorder, context = queue_case(tmp_path, (1, ["by:alice"]))
    context._scope = orch_scope.no_snapshot(context)
    assert context._scope.owns(tracker.get_issue(1)) is False
    assert orch_scope.may_write(context, 1) is False


@pytest.mark.parametrize("labels, legacy, mention", [
    ([], None, "@alice "), (["by:alice"], "alice", "@alice "), ([], "alice", "@alice ")])
def test_every_question_names_its_orderer(tmp_path, labels, legacy, mention):
    _, _, _, context = queue_case(tmp_path, (1, labels), legacy=legacy)
    assert orch_scope.ask_orderer(context, 1, "Which?") == f"{mention}Which?"


def test_a_failed_refresh_stops_orphan_recovery_on_a_stale_row(git_history, tmp_path, monkeypatch):
    from foragent import scheduler_recovery

    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    case.tracker.add_labels(1, ["running"])
    case.ctx.tracker.get_issue(1)
    ctx = handoff.tick_context(case)
    ctx._scope = orch_scope.no_snapshot(ctx)
    monkeypatch.setattr(scheduler, "_remove_run_worktree", lambda *a, **kw: None)
    stale = next(row for row in ctx.store.local_issues() if row["number"] == 1)
    ctx._scope.owns(orch_scope._model(1, stale))
    scheduler_recovery._recover_orphan(ctx, case.tracker.get_issue(1), scheduler.TickReport())
    assert "running" in case.tracker.get_issue(1).labels and "ready" not in case.tracker.get_issue(1).labels


def foreign_batch_pr(git_history, tmp_path, monkeypatch):
    case = batch_case(git_history, tmp_path, monkeypatch, "bob")
    case.ctx._scope = orch_scope.Scope(case.ctx)
    for pr in list(case.tracker.created_prs):
        case.tracker.close_pr(pr.number)
    return case, case.tracker.create_pr(head="foragent/issue-1", base="foragent/batch-99", title="Work", body="")


def test_a_review_refuses_a_pr_into_another_persons_batch(git_history, tmp_path, monkeypatch):
    from foragent import run_review

    case, _ = foreign_batch_pr(git_history, tmp_path, monkeypatch)
    assert run_review._review_only(1, case.ctx, case.store.worktree_path(1), None) == "base-changed"


def test_a_final_merge_refuses_a_pr_into_another_persons_batch(git_history, tmp_path, monkeypatch):
    from foragent import run_publish

    case, pr = foreign_batch_pr(git_history, tmp_path, monkeypatch)
    monkeypatch.setattr(run_publish, "_ci_status", lambda *a, **kw: "passed")
    inputs = run_publish._final_merge_inputs(1, case.ctx, case.ctx.git, pr, "foragent/issue-1", "foragent/batch-99",
                                             "head", case.store.load_approval(1), 0.0)
    assert inputs.stop is not None and inputs.stop() == "base-changed"


def test_an_implementation_gets_no_batch_of_another_person(git_history, tmp_path, monkeypatch):
    from foragent import run_implementation

    case = batch_case(git_history, tmp_path, monkeypatch, "bob")
    case.ctx._scope = orch_scope.Scope(case.ctx)
    git = case.ctx.git
    attempt = run_implementation._prepare_implementation(
        1, case.ctx, git, target_branch=case.ctx.config.base_branch, capture_baseline=False)
    assert attempt.batch_branch is None and attempt.parent is None


def unseen_case(tmp_path, owner):
    """A singleton factory (no legacy_owner) and #7 and its PR that no read has shown."""
    store, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.add_issue(7, "Unseen", "", labels=[f"by:{owner}"])
    return store, tracker, recorder, context


@pytest.mark.parametrize("owner, closed", [("bob", False), ("alice", True)])
def test_closing_an_unseen_issue_reads_its_owner_first(tmp_path, owner, closed):
    _, tracker, recorder, _ = unseen_case(tmp_path, owner)
    recorder.close_issue(7)
    assert (tracker.get_issue(7).state == "closed") is closed


def test_a_queued_close_of_an_unseen_issue_is_judged_at_replay(tmp_path):
    store, tracker, recorder, _ = unseen_case(tmp_path, "bob")
    store.queue_github_write(7, "close_issue", lanes={}, body=None)
    recorder.flush_outbox()
    assert tracker.get_issue(7).state == "open"
    assert [entry["issue"] for entry in store.refused_github_writes()] == [7]


@pytest.mark.parametrize("act", [
    lambda r, pr: r.close_pr(pr), lambda r, pr: r.merge_pr(pr, expected_head_sha="x"),
    lambda r, pr: r.update_pr(pr, base="main", title="T", body="B")])
def test_changing_an_unseen_pr_of_another_person_is_refused(tmp_path, act):
    from foragent.tracker import WriteRefused

    _, tracker, recorder, _ = unseen_case(tmp_path, "bob")
    pr = tracker.create_pr(head="foragent/issue-7", base="main", title="Bob's", body="")
    with pytest.raises(WriteRefused):
        act(recorder, pr)
    assert tracker.merged_prs == [] and next(p for p in tracker.created_prs if p.number == pr).title == "Bob's"


def test_changing_an_unseen_pr_of_this_person_still_works(tmp_path):
    _, tracker, recorder, _ = unseen_case(tmp_path, "alice")
    pr = tracker.create_pr(head="foragent/issue-7", base="main", title="Mine", body="")
    recorder.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
    assert tracker.merged_prs == [pr]


def test_a_failed_read_refuses_an_unseen_close_and_reads_once(tmp_path):
    _, tracker, recorder, _ = unseen_case(tmp_path, "alice")
    reads, get_issue = [], tracker.get_issue

    def failing(number):
        reads.append(number)
        raise TrackerError("HTTP 502")

    tracker.get_issue = failing
    recorder.close_issue(7)
    tracker.get_issue = get_issue
    assert reads == [7] and tracker.get_issue(7).state == "open"


def test_a_singleton_issue_seen_in_the_snapshot_costs_no_read(tmp_path, monkeypatch):
    _, tracker, recorder, _ = queue_case(tmp_path, (1, []), legacy=None)
    monkeypatch.setattr(orch_scope, "_read_target", lambda *a: pytest.fail("no read for a seen target"))
    recorder.close_issue(1)
    assert tracker.issues[1].state == "closed"


@pytest.mark.parametrize("owner, refused", [("bob", True), ("alice", False)])
def test_a_pr_into_a_batch_is_judged_by_the_batch_owner(tmp_path, owner, refused):
    from foragent.tracker import WriteRefused

    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]), (99, [f"by:{owner}"]))
    if refused:
        with pytest.raises(WriteRefused):
            recorder.create_pr(head="foragent/issue-1", base="foragent/batch-99", title="T", body="")
        assert tracker.created_prs == []
    else:
        pr = recorder.create_pr(head="foragent/issue-1", base="foragent/batch-99", title="T", body="")
        recorder.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
        assert tracker.merged_prs == [pr]


def test_a_recorded_batch_base_of_another_person_stops_the_merge(tmp_path):
    from foragent.tracker import WriteRefused

    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]), (99, ["by:bob"]))
    pr = tracker.create_pr(head="foragent/issue-1", base="foragent/batch-99", title="T", body="")
    store.update_local_issue(1, pr_number=pr, pr_base="foragent/batch-99")
    with pytest.raises(WriteRefused):
        recorder.merge_pr(pr, expected_head_sha="x")
    assert tracker.merged_prs == []


def test_orch_merge_refuses_a_child_pr_into_another_persons_batch(merge_case, monkeypatch):
    from foragent import orch

    case = merge_case
    object.__setattr__(case.ctx.config, "legacy_owner", None)
    case.tracker.add_issue(99, "Bob's parent", "", labels=["by:bob"])
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.ctx.tracker.get_issue(99)
    pr = case.pr if hasattr(case, "pr") else case.tracker.created_prs[-1].number
    current = next(p for p in case.tracker.created_prs if p.number == pr)
    monkeypatch.setattr(orch, "_open_pr", lambda ctx, n: replace(current, base="foragent/batch-99"))
    monkeypatch.setattr(orch, "_premerge", lambda ctx, n: SimpleNamespace(
        refused=None, report={"ready": True}, pr=replace(current, base="foragent/batch-99"),
        base=None, head=case.tracker.pr_head_sha(pr), approved=None))
    monkeypatch.setattr(orch, "_approved_inputs", lambda *a: None)
    monkeypatch.setattr(case.ctx.git, "fetch", lambda *a: None)
    monkeypatch.setattr(case.ctx.git, "ref_sha", lambda ref: None)
    with pytest.raises(orch.OrchError, match="bob"):
        orch.merge(case.ctx, pr, approved_by="alice", at=NOW.isoformat())
    assert case.tracker.merged_prs == []


def test_a_question_nobody_can_be_named_for_is_not_posted(tmp_path):
    _, tracker, _, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.authenticated_login = lambda **kw: (_ for _ in ()).throw(RuntimeError("signed out"))
    with pytest.raises(orch_scope.OrdererUnknown):
        orch_scope.ask_orderer(context, 1, "Which?")


def test_a_run_question_without_an_orderer_stays_in_the_log(run_case):
    case = run_case
    case.executor.steps = [{".foragent/QUESTION.md": "Choose A or B."}]
    case.tracker.authenticated_login = lambda **kw: (_ for _ in ()).throw(RuntimeError("signed out"))
    assert runner.run_issue(1, case.ctx) == "error"
    bodies = [comment.body for comment in case.tracker.list_comments(1)]
    assert not any("product decision" in body or "Choose A or B" in body for body in bodies)
    assert any("Cannot name who to ask" in body for body in bodies)
    assert "Choose A or B." in case.store.log_path(1).read_text()


def test_a_tick_holds_a_cancelled_blocker_question_it_cannot_address(tmp_path, monkeypatch):
    from foragent import scheduler_checks
    from foragent.tracker import Issue

    _, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.authenticated_login = lambda **kw: (_ for _ in ()).throw(RuntimeError("signed out"))
    context.tracker = recorder
    monkeypatch.setattr(scheduler, "_blockers", lambda ctx, n, confirmed=False: [
        Issue(7, "Gone", "", frozenset(), "closed", state_reason="not_planned")])
    report = scheduler.TickReport()
    issue = tracker.get_issue(1)
    approval = SimpleNamespace(body=issue.body, parsed=None)
    monkeypatch.setattr(scheduler_checks.scheduler, "has_machine_check", lambda parsed: True)
    monkeypatch.setattr(scheduler_checks.scheduler, "parse_body", lambda body: None, raising=False)
    assert scheduler._check_prerequisites(context, issue, approval, report) is False
    assert tracker.list_comments(1) == [] and any("Cannot name" in line for line in report.actions)


def test_a_target_this_operation_already_read_is_not_read_again(tmp_path, monkeypatch):
    _, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.add_issue(8, "Entered", "", labels=["by:alice"])
    context._scope = orch_scope.Scope(context, tracker.get_issue(8))  # An entry read, with no stored row.
    monkeypatch.setattr(orch_scope, "_read_target", lambda *a: pytest.fail("the entry read already showed #8"))
    assert context._scope.for_effect(8, tracker) is None


def test_an_unseen_target_costs_one_read(tmp_path, monkeypatch):
    _, tracker, _, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.add_issue(7, "Bob's", "", labels=["by:bob"])
    reads, read = [], orch_scope._read_target
    monkeypatch.setattr(orch_scope, "_read_target", lambda *a: reads.append(a[2]) or read(*a))
    snapshot = orch_scope.scope(context)
    assert "bob" in snapshot.for_effect(7, tracker) and "bob" in snapshot.for_effect(7, tracker)
    assert reads == [7]


def test_an_unseen_batch_parent_of_another_person_is_not_used(tmp_path):
    _, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.add_issue(99, "Bob's parent", "", labels=["by:bob"])
    tracker.set_parent(1, 99)
    assert orch_scope.batch_parent(context, 1) is None


class FakeGit:
    """Only what WIP preservation calls; it records pushes."""

    def __init__(self):
        self.local_skill_paths, self.pushed = set(), []

    def conflict_paths(self):
        return []

    def stage_changes(self):
        pass

    def has_staged_changes(self):
        return False

    def head_sha(self):
        return "wip"

    def push(self, branch, **kw):
        self.pushed.append(branch)


@pytest.mark.parametrize("snapshot, pushed", [("none", []), ("own", ["foragent/issue-1"])])
def test_wip_is_pushed_only_with_this_persons_snapshot(tmp_path, snapshot, pushed):
    from foragent import run_publish

    store, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None)
    tracker.add_labels(1, ["by:bob"])  # Bob's on GitHub now; the stored row is still unlabelled.
    context._scope = orch_scope.no_snapshot(context) if snapshot == "none" else orch_scope.Scope(context)
    git = FakeGit()
    run_publish._preserve_locked_implementation(
        1, context, git, "crashed", approval=None, approved_ref="origin/main", title="T", pushed_head=None,
        tracker_available=True, push_branch=None, owned_deletions=True)
    assert git.pushed == pushed
    if not pushed:
        assert "WIP kept on local branch" in store.log_path(1).read_text()


def test_a_run_never_pushes_a_branch_without_a_snapshot(tmp_path):
    from foragent import run_publish
    from foragent.run_attempt import OwnershipLost

    _, _, _, context = queue_case(tmp_path, (1, []), legacy=None)
    context._scope = orch_scope.no_snapshot(context)
    git = FakeGit()
    attempt = SimpleNamespace(ctx=context, n=1, rebuilt=False, git=git, branch="foragent/issue-1")
    with pytest.raises(OwnershipLost):
        run_publish._push_branch(attempt)
    assert git.pushed == []
    context._scope = orch_scope.Scope(context)
    run_publish._push_branch(attempt)
    assert git.pushed == ["foragent/issue-1"]

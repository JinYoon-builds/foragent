from dataclasses import replace
from datetime import UTC, datetime, timedelta
import subprocess

import pytest

from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.reasons import Reason, parse_approval_comment, parse_reason
from foragent.scheduler import TickContext, flag, runner_process_exists, tick
from foragent.state import StateStore
from foragent.tracker import Dependency, FakeTracker, LabelEvent, TrackerError

from conftest import ACCEPTANCE, git_command


NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BODY = f"""<!-- foragent:v1 -->
## Goal
Implement the feature.
## Out of scope
Other features.
## Done when
```sh
pytest {ACCEPTANCE}
```
"""


@pytest.fixture
def ctx(git_history, tmp_path, monkeypatch):
    context = TickContext(
        repo="owner/repo", repo_root=git_history.clone,
        tracker=FakeTracker(strict=True), store=StateStore(tmp_path / "state"),
        config=Config(approvers=["human"]), git=GitRepo(git_history.clone), now=lambda: NOW,
        launcher=lambda number: 7000 + number, start_runs=False,
        runner_process_exists=lambda number: False,
    )
    context.tracker._now = lambda: context.now()
    context.store._now = lambda: context.now()
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    return context


def approve(ctx, number=1, *, body=BODY, actor="human", labels=()):
    ctx.tracker.add_issue(number, f"Issue {number}", body, labels=labels)
    ctx.tracker.record_label(number, "approved", actor)


def edit_body(ctx, number, body):
    ctx.tracker.edit_body(number, body)


@pytest.mark.parametrize("actor,allowed", [("human", True), ("HuMaN", True), ("stranger", False)])
def test_approval_actor_and_snapshot(ctx, actor, allowed):
    approve(ctx, actor=actor)
    report = tick(ctx)
    assert report.skipped is None
    issue = ctx.tracker.get_issue(1)
    if allowed:
        assert issue.labels == {"approved", "ready"}
        approval = ctx.store.load_approval(1)
        assert approval.body == BODY and approval.body_sha == body_hash(BODY)
        assert approval.approved_by == actor and approval.approved_at == NOW.isoformat()
        assert approval.acceptance_blobs == ctx.git.blob_shas("origin/main", [ACCEPTANCE])
        assert parse_approval_comment(ctx.tracker.comments[1][0].body)["body_sha"] == body_hash(BODY)
        assert "#1 promoted to ready" in report.actions
        assert tick(ctx).actions == []
        assert len(ctx.tracker.comments[1]) == 1
    else:
        assert issue.labels == {"approved"}
        assert ctx.store.load_approval(1) is None
        assert ctx.tracker.list_comments(1) == []
        assert len(report.actions) == 1 and "non-approver" in report.actions[0]
        assert len(tick(ctx).actions) == 1


def test_latest_valid_approval_ignores_other_actors(ctx):
    approve(ctx)
    ctx.tracker._now = lambda: NOW + timedelta(seconds=1)
    ctx.tracker.record_label(1, "approved", "stranger")
    report = tick(ctx)
    assert ctx.store.load_approval(1).approved_at == NOW.isoformat()
    assert len([line for line in report.actions if "non-approver" in line]) == 1


def test_latest_approver_event_is_the_snapshot_identity(ctx):
    approve(ctx)
    later = NOW + timedelta(minutes=1)
    ctx.tracker._now = lambda: later
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    assert ctx.store.load_approval(1).approved_at == later.isoformat()


def test_removed_approval_and_unauthorized_readdition_do_not_start(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.tracker.record_label(1, "approved", "stranger")
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == []
    assert "running" not in ctx.tracker.get_issue(1).labels


def test_removed_then_reapproved_takes_new_snapshot(ctx):
    approve(ctx)
    tick(ctx)
    original = ctx.store.load_approval(1)
    ctx.tracker.remove_labels(1, ["approved"])
    edit_body(ctx, 1, BODY.replace("Implement", "Improve"))
    assert tick(ctx).actions == []
    ctx.tracker._now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    approval = ctx.store.load_approval(1)
    assert approval.approved_at != original.approved_at
    assert approval.body != original.body
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert len(ctx.tracker.list_comments(1)) == 2


@pytest.mark.parametrize("state", ["waiting", "ready", "running"])
def test_body_integrity_for_every_approved_state(ctx, state):
    approve(ctx)
    tick(ctx)
    if state == "waiting":
        ctx.tracker.remove_labels(1, ["ready"])
    elif state == "running":
        ctx.tracker.remove_labels(1, ["ready"])
        ctx.tracker.add_labels(1, ["running"])
        ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1), pid=123)
    edit_body(ctx, 1, BODY.replace("Other features.", "Changed scope."))
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    assert ctx.store.load_approval(1) is None
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.HASH_MISMATCH
    assert "body changed" in comment and "Re-approve" in comment
    if state == "running":
        assert any("already running" in line for line in report.actions)
        assert ctx.store.active_runs()[0].pid == 123
    assert tick(ctx).actions == []


@pytest.mark.parametrize("content", ["def test_feature():\n    pass\n", None])
def test_acceptance_integrity_tracks_origin_changes_and_deletions(ctx, git_history, content):
    approve(ctx)
    tick(ctx)
    git_history.update({ACCEPTANCE: content})
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    assert ctx.store.load_approval(1) is None
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.HASH_MISMATCH
    assert ACCEPTANCE in comment and "Acceptance file changed" in comment


def test_integrity_details_include_body_and_all_changed_files(ctx, git_history):
    second = "tests/acceptance/second.py"
    git_history.update({second: "assert True\n"})
    approve(ctx, body=BODY.replace(f"pytest {ACCEPTANCE}", f"pytest {ACCEPTANCE} {second}"))
    tick(ctx)
    edit_body(ctx, 1, ctx.tracker.get_issue(1).body + "Edited\n")
    git_history.update({ACCEPTANCE: "pass\n", second: "pass\n"})
    tick(ctx)
    comment = ctx.tracker.list_comments(1)[-1].body
    assert "Issue body changed" in comment
    assert ACCEPTANCE in comment and second in comment


def test_missing_acceptance_file_is_snapshotted_and_detected_when_added(ctx, git_history):
    path = "tests/acceptance/not_yet.py"
    approve(ctx, body=BODY.replace(ACCEPTANCE, path))
    tick(ctx)
    assert ctx.store.load_approval(1).acceptance_blobs == {path: "missing"}
    git_history.update({path: "assert True\n"})
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}


def test_no_machine_check_flags_once_and_human_removal_resumes(ctx):
    approve(ctx, body="## Done when\n- [ ] Looks good\n")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.NO_MACHINE_CHECK
    count = len(ctx.tracker.list_comments(1))
    assert tick(ctx).actions == []
    assert len(ctx.tracker.list_comments(1)) == count
    ctx.tracker.remove_labels(1, ["needs-human", "approved"])
    edit_body(ctx, 1, BODY)
    ctx.tracker._now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_needs_human_is_skipped_entirely(ctx, monkeypatch):
    approve(ctx, labels=["needs-human", "ready"])

    def unexpected(*args):
        pytest.fail("needs-human issues must not be inspected")

    monkeypatch.setattr(ctx.tracker, "label_events", unexpected)
    monkeypatch.setattr(ctx.tracker, "blocked_by", unexpected)
    assert tick(ctx).actions == []
    assert ctx.store.load_approval(1) is None


def test_blocked_by_waits_until_all_blockers_close(ctx):
    ctx.tracker.add_issue(1)
    approve(ctx, 2)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "open"), Dependency(3, "closed")])
    tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved"}
    ctx.tracker.close_issue(1)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "closed"), Dependency(3, "closed")])
    tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_one_run_per_project_still_promotes_other_issues(ctx):
    approve(ctx, 1)
    approve(ctx, 2)
    active = ctx.store.start_run(99, branch="active", worktree="active", pid=999)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == [active]
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_lowest_number_starts_and_launcher_pid_is_stored(ctx):
    for number in (9, 4, 2):
        approve(ctx, number, labels=["ready"] if number == 4 else [])
    calls = []

    def launch(number):
        calls.append(number)
        run = ctx.store.get_run(number)
        assert run is not None and run.pid is None
        assert "running" in ctx.tracker.get_issue(number).labels
        return 4321

    ctx.launcher = launch
    ctx.start_runs = True
    report = tick(ctx)
    assert calls == [2]
    assert ctx.tracker.get_issue(2).labels == {"approved", "running"}
    assert ctx.tracker.get_issue(4).labels == ctx.tracker.get_issue(9).labels == {"approved", "ready"}
    run = ctx.store.get_run(2)
    assert run.pid == 4321 and run.branch == "foragent/issue-2"
    assert run.worktree == str(ctx.store.worktree_path(2))
    assert [line for line in report.actions if "promoted" in line] == [
        "#2 promoted to ready", "#9 promoted to ready",
    ]


def test_launcher_can_return_no_pid(ctx):
    approve(ctx)
    ctx.start_runs = True
    ctx.launcher = lambda number: None
    tick(ctx)
    assert ctx.store.get_run(1).pid is None


def test_outside_local_run_hours_promotes_without_starting(ctx):
    approve(ctx)
    local_hour = ctx.now().astimezone().hour
    start = (local_hour + 1) % 24
    end = (local_hour + 2) % 24
    ctx.config = replace(ctx.config, run_hours=f"{start:02d}-{end:02d}")
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.active_runs() == []


def test_no_start_promotes_without_launching(ctx):
    approve(ctx)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.active_runs() == []


def test_locked_tick_has_no_git_or_tracker_side_effects(ctx, monkeypatch):
    def unexpected(*args):
        pytest.fail("a skipped tick must do no work")

    monkeypatch.setattr(ctx.git, "fetch", unexpected)
    monkeypatch.setattr(ctx.tracker, "list_open_issues", unexpected)
    with ctx.store.tick_lock() as acquired:
        assert acquired
        report = tick(ctx)
    assert report.skipped == "another tick is running" and report.actions == []


def test_fetches_configured_base_once_and_holds_tick_lock_through_launch(ctx, monkeypatch):
    approve(ctx)
    ctx.start_runs = True
    calls = []
    original_fetch = ctx.git.fetch

    def fetch(remote, ref):
        calls.append((remote, ref))
        with ctx.store.tick_lock() as acquired:
            assert not acquired
        original_fetch(remote, ref)

    def launch(number):
        with ctx.store.tick_lock() as acquired:
            assert not acquired
        return 123

    monkeypatch.setattr(ctx.git, "fetch", fetch)
    ctx.launcher = launch
    tick(ctx)
    assert calls == [("origin", "main")]


def test_integrity_rechecked_against_fresh_issue_right_before_start(ctx, monkeypatch):
    approve(ctx)
    ctx.start_runs = True
    original = ctx.tracker.get_issue
    calls = []

    def get_issue(number):
        issue = original(number)
        if "ready" in issue.labels:
            calls.append(number)
            return replace(issue, body=issue.body + "Changed before start\n")
        return issue

    monkeypatch.setattr(ctx.tracker, "get_issue", get_issue)
    tick(ctx)
    assert calls
    assert ctx.store.active_runs() == []
    assert original(1).labels == {"needs-human"}
    assert ctx.store.load_approval(1) is None


def test_acceptance_blobs_rechecked_before_start(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    ctx.start_runs = True
    original = ctx.git.blob_shas
    calls = []

    def blob_shas(ref, paths):
        calls.append(ref)
        blobs = original(ref, paths)
        return blobs if len(calls) == 1 else {ACCEPTANCE: "changed"}

    monkeypatch.setattr(ctx.git, "blob_shas", blob_shas)
    tick(ctx)
    assert len(calls) == 2
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}


@pytest.mark.parametrize("change", ["closed", "needs-human", "unapproved", "reapproved"])
def test_issue_must_still_be_eligible_before_start(ctx, monkeypatch, change):
    approve(ctx)
    tick(ctx)
    ctx.start_runs = True
    original = ctx.tracker.get_issue

    def get_issue(number):
        issue = original(number)
        if change == "closed":
            return replace(issue, state="closed")
        if change == "needs-human":
            return replace(issue, labels=issue.labels | {"needs-human"})
        if change == "unapproved":
            return replace(issue, labels=issue.labels - {"approved"})
        event = next(event for event in ctx.tracker.label_events(number) if event.label == "approved")
        ctx.tracker._events[number].append(
            replace(event, created_at=(NOW + timedelta(minutes=1)).isoformat())
        )
        return issue

    monkeypatch.setattr(ctx.tracker, "get_issue", get_issue)
    tick(ctx)
    assert ctx.store.active_runs() == []


def test_flag_only_changes_labels_and_posts_a_reason(ctx):
    approve(ctx, labels=["ready", "running"])
    original = ctx.tracker.get_issue(1)
    flag(ctx, 1, Reason.SPEC_QUESTION, "Choose a behavior.", "Option A or B.")
    issue = ctx.tracker.get_issue(1)
    assert issue.labels == {"approved", "needs-human"}
    assert issue.body == original.body and issue.state == "open"
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.SPEC_QUESTION
    assert ctx.tracker.created_prs == ctx.tracker.merged_prs == []


def test_approver_config_is_case_insensitive(ctx):
    ctx.config = replace(ctx.config, approvers=["HUMAN"])
    approve(ctx, actor="human")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("edit_seconds", [-1, 0, 1])
def test_first_snapshot_rejects_edits_at_or_after_approval(ctx, edit_seconds):
    approve(ctx)
    ctx.tracker._now = lambda: NOW + timedelta(seconds=edit_seconds)
    edit_body(ctx, 1, BODY + "Edited\n")
    ctx.now = lambda: NOW + timedelta(minutes=1)
    tick(ctx)
    if edit_seconds >= 0:
        assert ctx.store.load_approval(1) is None
        assert ctx.tracker.get_issue(1).labels == {"needs-human"}
        assert len(ctx.tracker.list_comments(1)) == 1
        comment = ctx.tracker.list_comments(1)[0].body
        assert parse_reason(comment) == Reason.HASH_MISMATCH
        assert "approval predates an edit" in comment
        assert ctx.store.load_flag(1).flagged_at == ctx.now().isoformat()
    else:
        assert ctx.store.load_approval(1).body == BODY + "Edited\n"
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_body_changed_and_reverted_before_first_tick_still_requires_reapproval(ctx):
    approve(ctx)
    ctx.now = lambda: NOW + timedelta(seconds=1)
    edit_body(ctx, 1, BODY + "Edited\n")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    edit_body(ctx, 1, BODY)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}


@pytest.mark.parametrize("content", ["pass\n", None])
def test_acceptance_changed_after_approval_before_first_tick(ctx, git_history, content):
    approve(ctx)
    git_history.update({ACCEPTANCE: content}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    comments = ctx.tracker.list_comments(1)
    assert len(comments) == 1 and parse_reason(comments[0].body) == Reason.HASH_MISMATCH
    assert ACCEPTANCE in comments[0].body


def test_acceptance_snapshot_uses_approval_commit_and_allows_unrelated_changes(ctx, git_history):
    git_history.update({ACCEPTANCE: "assert 1 == 1\n"}, at=NOW - timedelta(minutes=1))
    approved_ref = git_history.tip
    approve(ctx)
    git_history.update({"unrelated.py": "pass\n"}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    approval = ctx.store.load_approval(1)
    assert approval.acceptance_blobs == ctx.git.blob_shas(approved_ref, [ACCEPTANCE])
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_missing_approval_time_base_commit_requires_reapproval(ctx, monkeypatch):
    approve(ctx)
    monkeypatch.setattr(ctx.git, "commit_before", lambda ref, time: None)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    assert "no base commit" in ctx.tracker.list_comments(1)[0].body


def test_hash_mismatch_reapproval_is_one_human_action_and_survives_restart(ctx):
    approve(ctx)
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=1)
    edit_body(ctx, 1, BODY + "Changed\n")
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    assert ctx.store.load_flag(1).reason == Reason.HASH_MISMATCH
    ctx.store = StateStore(ctx.store.root, now=lambda: ctx.now())
    ctx.now = lambda: NOW + timedelta(minutes=3)
    ctx.tracker.record_label(1, "approved", "HUMAN")
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.load_approval(1).approved_at == ctx.now().isoformat()
    assert ctx.store.load_approval(1).body == BODY + "Changed\n"
    assert "#1 resumed after re-approval" in report.actions


@pytest.mark.parametrize("event_offset", [-1, 0])
def test_reapproval_must_be_strictly_after_flag(ctx, event_offset):
    approve(ctx)
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=2)
    flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
    ctx.tracker._now = lambda: NOW + timedelta(minutes=2, seconds=event_offset)
    ctx.tracker.record_label(1, "approved", "human")
    ctx.now = lambda: NOW + timedelta(minutes=3)
    assert tick(ctx).actions == []
    assert "needs-human" in ctx.tracker.get_issue(1).labels


@pytest.mark.parametrize("reason", [Reason.NO_MACHINE_CHECK, Reason.SPEC_QUESTION, Reason.RETRIES_EXCEEDED])
def test_other_flags_require_human_removal_even_with_new_approval(ctx, reason):
    approve(ctx)
    flag(ctx, 1, reason, "Human input required.")
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    assert tick(ctx).actions == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert ctx.store.load_approval(1) is None
    ctx.tracker.remove_labels(1, ["needs-human"])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_nonapprover_cannot_resume_hash_mismatch(ctx):
    approve(ctx)
    flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "stranger")
    tick(ctx)
    assert "needs-human" in ctx.tracker.get_issue(1).labels
    assert ctx.store.load_approval(1) is None


@pytest.mark.parametrize("needs_human", [False, True])
def test_approval_event_without_current_label_is_invalid(ctx, needs_human):
    approve(ctx)
    if needs_human:
        flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
        ctx.now = lambda: NOW + timedelta(minutes=1)
        ctx.tracker.record_label(1, "approved", "human")
    issue = ctx.tracker.get_issue(1)
    ctx.tracker.issues[1] = replace(issue, labels=issue.labels - {"approved"})
    assert tick(ctx).actions == []
    assert ctx.store.load_approval(1) is None


def test_current_label_without_valid_timeline_is_invalid(ctx):
    approve(ctx)
    ctx.tracker._events[1].append(LabelEvent(
        "approved", "unlabeled", "human", (NOW + timedelta(minutes=1)).isoformat(),
    ))
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert "ready" not in ctx.tracker.get_issue(1).labels


def mark_running(ctx, number=1, pid=123):
    ctx.tracker.remove_labels(number, ["ready"])
    ctx.tracker.add_labels(number, ["running"])
    return ctx.store.start_run(
        number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), pid=pid,
    )


def dead_process(pid, signal):
    raise ProcessLookupError(pid)


def test_dead_pid_finishes_run_and_restores_ready(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    calls = []

    def kill(pid, signal):
        calls.append((pid, signal))
        raise ProcessLookupError(pid)

    monkeypatch.setattr("foragent.scheduler.os.kill", kill)
    report = tick(ctx)
    assert calls == [(123, 0)]
    assert ctx.store.active_runs() == []
    assert ctx.store.read_history()[0].outcome == "crashed"
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert "#1 runner crashed" in report.actions


@pytest.mark.parametrize("permission_denied", [False, True])
def test_alive_or_inaccessible_pid_keeps_project_slot(ctx, monkeypatch, permission_denied):
    approve(ctx)
    tick(ctx)
    active = mark_running(ctx)
    calls = []

    def kill(pid, signal):
        calls.append((pid, signal))
        if permission_denied:
            raise PermissionError(pid)

    monkeypatch.setattr("foragent.scheduler.os.kill", kill)
    ctx.now = lambda: NOW + timedelta(hours=1)
    ctx.start_runs = True
    tick(ctx)
    assert calls == [(123, 0)]
    assert ctx.store.active_runs() == [active]
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}


@pytest.mark.parametrize("elapsed,crashed", [(119, False), (120, True), (121, True)])
def test_pid_none_has_two_minute_startup_grace(ctx, monkeypatch, elapsed, crashed):
    approve(ctx)
    tick(ctx)
    active = mark_running(ctx, pid=None)

    def unexpected(*args):
        pytest.fail("no pid is available to probe")

    monkeypatch.setattr("foragent.scheduler.os.kill", unexpected)
    probes = []
    ctx.runner_process_exists = lambda number: probes.append(number) or False
    ctx.now = lambda: NOW + timedelta(seconds=elapsed)
    tick(ctx)
    assert probes == ([1] if crashed else [])
    if crashed:
        assert ctx.store.active_runs() == []
        assert ctx.store.get_run(1).outcome == "crashed"
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    else:
        assert ctx.store.active_runs() == [active]


def test_two_consecutive_crashes_flag_and_stop_relaunching(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    monkeypatch.setattr("foragent.scheduler.os.kill", dead_process)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.start_runs = True
    ctx.now = lambda: NOW + timedelta(minutes=1)
    tick(ctx)
    assert launches == [1] and ctx.store.get_run(1).pid == 456
    ctx.now = lambda: NOW + timedelta(minutes=2)
    report = tick(ctx)
    assert launches == [1] and ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed", "crashed"]
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.RETRIES_EXCEEDED
    assert ctx.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "#1 flagged retries-exceeded" in report.actions
    assert tick(ctx).actions == []


@pytest.mark.parametrize("intervening_number,expected_flag", [(1, False), (2, True)])
def test_crash_streak_is_per_issue_and_resets_on_another_outcome(ctx, monkeypatch, intervening_number, expected_flag):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.store.finish_run(1, "crashed")
    ctx.now = lambda: NOW + timedelta(seconds=1)
    ctx.store.start_run(intervening_number, branch="other", worktree="other")
    ctx.store.finish_run(intervening_number, "not-implemented")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(seconds=3)
    monkeypatch.setattr("foragent.scheduler.os.kill", dead_process)
    tick(ctx)
    assert ("needs-human" in ctx.tracker.get_issue(1).labels) == expected_flag
    assert ("ready" in ctx.tracker.get_issue(1).labels) != expected_flag


@pytest.mark.parametrize("state", ["closed", "needs-human"])
def test_dead_run_finishes_without_requeueing_closed_or_flagged_issue(ctx, monkeypatch, state):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    if state == "closed":
        ctx.tracker.close_issue(1)
    else:
        flag(ctx, 1, Reason.SPEC_QUESTION, "Human decision required.")
    monkeypatch.setattr("foragent.scheduler.os.kill", dead_process)
    tick(ctx)
    assert ctx.store.active_runs() == []
    assert ctx.store.get_run(1).outcome == "crashed"
    assert not ctx.tracker.get_issue(1).labels & {"ready", "running"}


def test_cancelled_blocker_flags_once_instead_of_promoting(ctx):
    approve(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "open"), Dependency(3, "closed", "not_planned")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.SPEC_QUESTION
    assert "blocker #3 was closed as not planned" in comment
    count = len(ctx.tracker.list_comments(1))
    assert tick(ctx).actions == []
    assert len(ctx.tracker.list_comments(1)) == count


def test_completed_blocker_allows_promotion(ctx):
    approve(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("command,exists", [
    ("/usr/bin/python -m foragent.cli gate run-issue 1 --repo-root /repo", True),
    ("/repo/.venv/bin/foragent gate run-issue 1 --repo-root /repo", True),
    ("/repo/.venv/bin/fa gate run-issue 1 --repo-root /repo with spaces", True),
    ("/usr/bin/python -m foragent.cli gate run-issue 11 --repo-root /repo", False),
    ("/usr/bin/python -m foragent.cli gate run-issue 2 --repo-root /repo", False),
    ("/repo/.venv/bin/fa gate status --repo-root /repo", False),
    ("/usr/bin/other run-issue 1", False),
    ("/usr/bin/notforagent gate run-issue 1 --repo-root /repo", False),
])
def test_process_probe_parses_ps_and_matches_exact_issue(monkeypatch, command, exists):
    def ps(args, **kwargs):
        assert args == ["ps", "-axo", "pid,command"]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(args, 0, stdout=f"  PID COMMAND\n  1234 {command}\n")

    monkeypatch.setattr("foragent.scheduler.subprocess.run", ps)
    assert runner_process_exists(1) is exists


@pytest.mark.parametrize("failure", [
    FileNotFoundError("ps unavailable"), PermissionError("ps denied"),
    subprocess.CalledProcessError(1, ["ps"]),
])
def test_process_probe_cannot_declare_death_when_ps_fails(monkeypatch, failure):
    def ps(*args, **kwargs):
        raise failure

    monkeypatch.setattr("foragent.scheduler.subprocess.run", ps)
    assert runner_process_exists(1)


@pytest.mark.parametrize("elapsed", [120, 3600])
def test_missing_pid_with_live_runner_keeps_project_slot(ctx, elapsed):
    approve(ctx)
    approve(ctx, 2)
    tick(ctx)
    active = mark_running(ctx, pid=None)
    probes = []
    launches = []
    ctx.runner_process_exists = lambda number: probes.append(number) or True
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.now = lambda: NOW + timedelta(seconds=elapsed)
    ctx.start_runs = True
    tick(ctx)
    assert probes == [1] and launches == []
    assert ctx.store.active_runs() == [active]
    assert ctx.store.read_history() == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_pid_persistence_failure_does_not_launch_a_second_runner(ctx, monkeypatch):
    approve(ctx)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.start_runs = True

    def fail(*args, **kwargs):
        raise OSError("pid persistence failed")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.store, "update_run", fail)
        with pytest.raises(OSError, match="pid persistence failed"):
            tick(ctx)
    active = ctx.store.get_run(1)
    assert active.pid is None and launches == [1]
    ctx.runner_process_exists = lambda number: True
    ctx.now = lambda: NOW + timedelta(minutes=3)
    tick(ctx)
    assert launches == [1] and ctx.store.active_runs() == [active]
    assert ctx.store.read_history() == []


@pytest.mark.parametrize("approved", [False, True])
def test_orphan_running_without_record_returns_to_ready(ctx, approved):
    if approved:
        approve(ctx, labels=["running"])
    else:
        ctx.tracker.add_issue(1, body=BODY, labels=["running"])
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == ({"approved", "ready"} if approved else {"ready"})
    assert ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed"]
    assert "#1 orphaned running label recovered as crashed" in report.actions
    assert "#1 returned to ready after runner crash" in report.actions
    assert tick(ctx).actions == []
    assert len(ctx.store.read_history()) == 1


def test_two_distinct_orphan_running_labels_count_as_consecutive_crashes(ctx):
    approve(ctx, labels=["running"])
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.remove_labels(1, ["ready"])
    ctx.tracker.add_labels(1, ["running"])
    report = tick(ctx)
    assert ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed", "crashed"]
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert ctx.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "#1 flagged retries-exceeded" in report.actions
    assert tick(ctx).actions == []


@pytest.mark.parametrize("operation", ["get_issue", "add_labels", "remove_labels"])
def test_crash_recovery_replays_after_tracker_failures_without_double_counting(ctx, monkeypatch, operation):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    monkeypatch.setattr("foragent.scheduler.os.kill", dead_process)

    def unavailable(*args, **kwargs):
        raise TrackerError("HTTP 503")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.tracker, operation, unavailable)
        for _ in range(2):
            with pytest.raises(TrackerError, match="HTTP 503"):
                tick(ctx)
            assert ctx.store.active_runs() == []
            assert len(ctx.store.read_history()) == 1
    ctx.now = lambda: NOW + timedelta(minutes=1)
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed"]
    assert ctx.store.load_flag(1) is None
    assert "#1 orphaned running label recovered as crashed" in report.actions


def test_ready_issue_reapproved_without_machine_check_is_flagged(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.now = lambda: NOW + timedelta(seconds=1)
    edit_body(ctx, 1, "## Done when\n- [ ] Looks good\n")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    ctx.tracker.record_label(1, "approved", "human")
    ctx.start_runs = True
    report = tick(ctx)
    assert ctx.store.load_approval(1).body == "## Done when\n- [ ] Looks good\n"
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert ctx.store.active_runs() == []
    assert ctx.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK
    assert "#1 flagged no-machine-check" in report.actions


@pytest.mark.parametrize("reopened", [False, True])
@pytest.mark.parametrize("start_runs", [False, True])
def test_ready_issue_with_new_or_reopened_blocker_returns_to_approved_waiting(ctx, reopened, start_runs):
    approve(ctx)
    if reopened:
        ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "open")])
    ctx.start_runs = start_runs
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved"}
    assert ctx.store.active_runs() == [] and ctx.store.load_flag(1) is None
    assert "#1 returned to approved waiting for open blockers" in report.actions
    ctx.start_runs = False
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_ready_issue_with_cancelled_blocker_is_flagged(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "not_planned")])
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert ctx.store.active_runs() == []
    assert ctx.store.load_flag(1).reason == Reason.SPEC_QUESTION


@pytest.mark.parametrize("cancelled", [False, True])
def test_dependencies_are_rechecked_immediately_before_launch(ctx, monkeypatch, cancelled):
    approve(ctx)
    tick(ctx)
    calls = []

    def blocked_by(number):
        calls.append(number)
        if len(calls) == 1:
            return []
        return [Dependency(2, "closed", "not_planned") if cancelled else Dependency(2, "open")]

    monkeypatch.setattr(ctx.tracker, "blocked_by", blocked_by)
    ctx.start_runs = True
    tick(ctx)
    assert calls == [1, 1] and ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == ({"approved", "needs-human"} if cancelled else {"approved"})


def test_machine_check_is_rechecked_from_snapshot_immediately_before_launch(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    calls = []

    def has_machine_check(parsed):
        calls.append(parsed)
        return len(calls) == 1

    monkeypatch.setattr("foragent.scheduler.has_machine_check", has_machine_check)
    ctx.start_runs = True
    tick(ctx)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[1].done_when_commands == [f"pytest {ACCEPTANCE}"]
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert ctx.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK


def test_custom_fetch_refspec_cannot_hide_changed_acceptance_files(ctx, git_history):
    approve(ctx)
    tick(ctx)
    git_command(
        git_history.clone, "config", "remote.origin.fetch",
        "+refs/heads/other:refs/remotes/origin/other",
    )
    git_history.update({ACCEPTANCE: "assert False\n"}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"needs-human"}
    assert ctx.store.active_runs() == [] and ctx.store.load_approval(1) is None
    assert ctx.store.load_flag(1).reason == Reason.HASH_MISMATCH


@pytest.mark.parametrize("existing_snapshot", [False, True])
def test_new_approval_snapshots_fresh_body_after_stale_issue_list(ctx, monkeypatch, existing_snapshot):
    approve(ctx)
    if existing_snapshot:
        tick(ctx)
    list_open_issues = ctx.tracker.list_open_issues
    new_body = BODY.replace("Implement the feature.", "Implement the approved revision.")

    def stale_issues():
        issues = list_open_issues()
        ctx.now = lambda: NOW + timedelta(seconds=1)
        ctx.tracker.remove_labels(1, ["approved"])
        edit_body(ctx, 1, new_body)
        ctx.now = lambda: NOW + timedelta(seconds=2)
        ctx.tracker.record_label(1, "approved", "human")
        return issues

    monkeypatch.setattr(ctx.tracker, "list_open_issues", stale_issues)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.load_approval(1).body == new_body
    assert ctx.store.load_approval(1).approved_at == (NOW + timedelta(seconds=2)).isoformat()
    assert ctx.store.load_flag(1) is None
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    assert ctx.store.get_run(1).pid == 7001

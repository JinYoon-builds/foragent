"""Offline changes at the queue boundary must invalidate final merge inputs."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from foragent import run_publish, runner
from foragent.git import GitRepo
from runner_support import batch_setup, run_case  # noqa: F401
from conftest import ACCEPTANCE, git_command


@pytest.fixture(params=["implementation", "reviewed"])
def queued_publish(run_case, monkeypatch, request):
    case = run_case
    case.reviewed = request.param == "reviewed"
    if case.reviewed:
        case.ctx.config = replace(case.ctx.config, review="careful")
        assert runner.run_issue(1, case.ctx) == "pr-open"
        case.now[0] += timedelta(seconds=1)
    case.ctx.config = replace(case.ctx.config, review="fast", max_retries=0)
    case.tracker.set_pr_checks(2, "pass")
    case.head = case.tracker.pr_head_sha(2)
    case.held = False
    case.acquisitions = 0
    case.change = lambda: None
    case.sleeps = []
    case.merge_acquisitions = []
    lock = case.store.merge_lock
    sleep = case.ctx.sleep
    merge = case.tracker.merge_pr
    merge_lock = run_publish._merge_lock

    @contextmanager
    def active_lock(n, ctx):
        case.active_ctx = ctx
        with merge_lock(n, ctx):
            yield

    @contextmanager
    def queued_lock():
        assert not case.held
        case.acquisitions += 1
        case.change()
        with lock():
            case.held = True
            try:
                yield
            finally:
                case.held = False

    def unlocked_sleep(seconds):
        assert not case.held
        assert not case.tracker.merged_prs
        case.sleeps.append(seconds)
        sleep(seconds)
        case.tracker.set_pr_checks(2, "pass")

    def merge_pr(pr, **kwargs):
        assert case.held
        case.merge_acquisitions.append(case.acquisitions)
        return merge(pr, **kwargs)

    def unlocked_effect(function):
        def call(*args, **kwargs):
            assert not case.held
            return function(*args, **kwargs)
        return call

    monkeypatch.setattr(case.store, "merge_lock", queued_lock)
    monkeypatch.setattr(run_publish, "_merge_lock", active_lock)
    monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)
    monkeypatch.setattr(case.tracker, "pr_failed_logs", unlocked_effect(case.tracker.pr_failed_logs))
    monkeypatch.setattr(run_publish, "flag", unlocked_effect(run_publish.flag))
    monkeypatch.setattr(run_publish, "invalidate_approval", unlocked_effect(run_publish.invalidate_approval))
    case.ctx.sleep = unlocked_sleep
    case.publish = (lambda: runner.review_only(1, case.ctx, verdict_head=case.head, repair_ci=False)) if case.reviewed else (
        lambda: runner.run_issue(1, case.ctx)
    )
    return case


@pytest.mark.parametrize("status,outcome", [("fail", "ci-failed"), ("pending", "merged")])
def test_ci_change_during_final_lock_wait_prevents_merge(queued_publish, status, outcome):
    case = queued_publish

    def change():
        if case.acquisitions == 1:
            case.tracker.set_pr_checks(2, status)

    case.change = change
    assert case.publish() == outcome
    assert case.merge_acquisitions == ([2] if status == "pending" else [])
    assert case.sleeps == ([30] if status == "pending" else [])


def test_failed_final_ci_snapshot_is_consumed_after_unlock(queued_publish, monkeypatch):
    case = queued_publish
    checks = case.tracker.pr_checks

    def transient_failure(pr):
        # A subsequent read would be green, but the final locked read failed.
        return "fail" if case.held else checks(pr)

    monkeypatch.setattr(case.tracker, "pr_checks", transient_failure)
    assert case.publish() == "ci-failed"
    assert case.tracker.merged_prs == []
    assert case.sleeps == []


def test_final_hold_reads_ci_exactly_once(queued_publish, monkeypatch):
    case = queued_publish
    checks = case.tracker.pr_checks
    locked_reads = []

    def count_reads(pr):
        if case.held:
            locked_reads.append(case.acquisitions)
        return checks(pr)

    monkeypatch.setattr(case.tracker, "pr_checks", count_reads)
    assert case.publish() == "merged"
    assert locked_reads == [1]


@pytest.mark.parametrize("changed,outcome", [
    ("labels", "held"),
    ("tracker-paths", "held"),
    ("local-paths", "held"),
    ("acceptance-dir", "held"),
    ("always-human", "held"),
    ("review-mode", "pr-open"),
    ("review-paths", "pr-open"),
    ("configured-base", "base-changed"),
    ("remote-head", "held"),
    ("local-head", "held"),
    ("pr-base", "base-changed"),
    ("pr-head-branch", "base-changed"),
    ("pr-state", "base-changed"),
    ("approval-label", "hash-mismatch"),
    ("approval-snapshot", "hash-mismatch"),
    ("approval-acceptance-snapshot", "hash-mismatch"),
    ("approval-removed", "hash-mismatch"),
    ("acceptance-head", "hash-mismatch"),
    ("acceptance-base", "hash-mismatch"),
    ("runtime-budget", "timeout"),
    ("publication", "retries-exceeded"),
])
def test_each_changed_merge_input_is_rejected_after_queuing(queued_publish, monkeypatch, changed, outcome):
    case = queued_publish

    def change():
        if changed == "labels":
            case.tracker.add_labels(1, ["risk:high"])
        elif changed == "tracker-paths":
            case.tracker.set_pr_files(2, ["feature.txt", ACCEPTANCE])
        elif changed == "local-paths":
            original = GitRepo.changed_paths
            monkeypatch.setattr(GitRepo, "changed_paths", lambda git, ref: [*original(git, ref), ACCEPTANCE])
        elif changed == "acceptance-dir":
            case.active_ctx.config = replace(case.active_ctx.config, acceptance_dir="feature.txt")
        elif changed == "always-human":
            case.active_ctx.config = replace(case.active_ctx.config, always_human=["feature.txt"])
        elif changed == "review-mode":
            case.active_ctx.config = replace(case.active_ctx.config, review="careful")
        elif changed == "review-paths":
            case.active_ctx.config = replace(case.active_ctx.config, review_paths={"feature.txt": "careful"})
        elif changed == "configured-base":
            case.active_ctx.config = replace(case.active_ctx.config, base_branch="other")
        elif changed == "remote-head":
            case.tracker.set_pr_head_sha(2, "f" * 40)
        elif changed == "local-head":
            original = GitRepo.head_sha
            monkeypatch.setattr(GitRepo, "head_sha", lambda git: "f" * 40 if case.held else original(git))
        elif changed == "pr-base":
            pr, = case.tracker.created_prs
            case.tracker.update_pr(2, base="other", title=pr.title, body=pr.body)
        elif changed == "pr-head-branch":
            pr, = case.tracker.created_prs
            case.tracker.created_prs = [replace(pr, head="other")]
        elif changed == "pr-state":
            case.tracker.close_pr(2)
        elif changed == "approval-label":
            case.tracker.remove_labels(1, ["approved"])
        elif changed == "approval-snapshot":
            approval = replace(case.store.load_approval(1), body_sha="f" * 64)
            case.store.save_approval(**approval.__dict__)
        elif changed == "approval-acceptance-snapshot":
            approval = replace(case.store.load_approval(1), acceptance_blobs={ACCEPTANCE: "changed"})
            case.store.save_approval(**approval.__dict__)
        elif changed == "approval-removed":
            monkeypatch.setattr(case.store, "load_approval", lambda number: None)
        elif changed == "acceptance-head":
            original = GitRepo.blob_shas
            monkeypatch.setattr(GitRepo, "blob_shas", lambda git, ref, paths: (
                {ACCEPTANCE: "changed"} if git.ref_sha(ref) == git.head_sha() else original(git, ref, paths)
            ))
        elif changed == "acceptance-base":
            case.history.update({ACCEPTANCE: "def test_changed():\n    assert False\n"})
        elif changed == "runtime-budget":
            case.active_ctx.config = replace(case.active_ctx.config, max_runtime_minutes=0)
        elif changed == "publication":
            (case.store.worktree_path(1) / "feature.txt").write_text("<<<<<<< HEAD\n=======\n>>>>>>> other\n")
            git_command(case.store.worktree_path(1), "add", "--", "feature.txt")

    case.change = change
    if case.reviewed and changed == "acceptance-base":
        outcome = "base-changed"
    if case.reviewed and changed == "publication":
        outcome = "ci-failed"
    assert case.publish() == outcome
    assert case.acquisitions == 1
    assert case.tracker.merged_prs == []
    assert case.sleeps == []


def test_live_body_change_during_lock_wait_respects_the_execution_snapshot(queued_publish):
    case = queued_publish

    def change():
        issue = case.tracker.get_issue(1)
        case.tracker.update_issue(1, title=issue.title, body=issue.body + "\nChanged instructions\n")

    case.change = change
    assert case.publish() == ("hash-mismatch" if case.reviewed else "merged")
    assert case.merge_acquisitions == ([] if case.reviewed else [1])


def test_base_sha_change_during_lock_wait_requeues_before_merge(queued_publish, monkeypatch):
    case = queued_publish
    checks = []
    verify = run_publish._check_publication

    def observe(git, target, base_sha=None):
        checks.append((case.acquisitions, case.held))
        return verify(git, target, base_sha)

    def change():
        if case.acquisitions == 1:
            case.history.update({"src/new-base.py": "VALUE = 1\n"})

    monkeypatch.setattr(run_publish, "_check_publication", observe)
    case.change = change
    assert case.publish() == ("base-changed" if case.reviewed else "merged")
    assert case.merge_acquisitions == ([] if case.reviewed else [2])
    if not case.reviewed:
        assert (1, False) in checks, "The moved base must be verified after releasing the first lock"


def test_base_rewind_during_lock_wait_also_requires_requeuing(run_case, monkeypatch):
    case = run_case
    old_base = case.history.tip
    case.history.update({"docs/base.md": "Base before queuing\n"})
    lock = case.store.merge_lock
    merge = case.tracker.merge_pr
    acquisitions = []
    merged_at = []

    @contextmanager
    def rewind_once():
        acquisitions.append(True)
        if len(acquisitions) == 1:
            git_command(case.history.origin, "update-ref", "refs/heads/main", old_base)
        with lock():
            yield

    def merge_pr(pr, **kwargs):
        merged_at.append(len(acquisitions))
        return merge(pr, **kwargs)

    monkeypatch.setattr(case.store, "merge_lock", rewind_once)
    monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert merged_at == [2]


def test_workflow_registration_is_rechecked_after_queue_wait(queued_publish):
    case = queued_publish

    def change():
        if case.acquisitions == 1:
            workflow = case.store.worktree_path(1) / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True, exist_ok=True)
            workflow.write_text("name: CI\n")
            case.tracker.set_pr_checks(2, "none")

    case.change = change
    assert case.publish() == "merged"
    assert case.merge_acquisitions == [2]
    assert case.sleeps == [30]


def test_requeuing_same_head_preserves_ci_wait_budget(queued_publish):
    case = queued_publish
    case.change = lambda: case.tracker.set_pr_checks(2, "pending")
    assert case.publish() == "timeout"
    assert case.tracker.merged_prs == []
    assert len(case.sleeps) == 40
    assert sum(case.sleeps) == 1200


@pytest.mark.parametrize("reviewed", [False, True])
def test_batch_parent_change_during_lock_wait_invalidates_target(run_case, monkeypatch, reviewed):
    case = run_case
    batch_setup(case)
    if reviewed:
        case.tracker.add_labels(1, ["risk:high"])
        assert runner.run_issue(1, case.ctx) == "held"
        case.now[0] += timedelta(seconds=1)
        case.tracker.remove_labels(1, ["risk:high"])
    lock = case.store.merge_lock

    @contextmanager
    def changed_parent():
        case.tracker.set_parent(1, 98)
        with lock():
            yield

    monkeypatch.setattr(case.store, "merge_lock", changed_parent)
    outcome = (runner.review_only(1, case.ctx, verdict_head=case.tracker.pr_head_sha(100))
               if reviewed else runner.run_issue(1, case.ctx))
    assert outcome == "base-changed"
    assert case.tracker.merged_prs == []

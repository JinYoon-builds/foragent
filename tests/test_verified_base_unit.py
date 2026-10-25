"""Checked Git bases remain values across publication and replay races."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, BASE_TIME, git_command
from foragent import run_conflicts, run_publish
from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.run_attempt import ConflictBudgetExhausted
from foragent.run_base_move import BaseMove, fetch_move
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from runner_support import BODY


@pytest.fixture
def replay_case(git_history, tmp_path):
    git = GitRepo(git_history.clone)
    worktree = tmp_path / "worktree"
    branch = "foragent/issue-1"
    git.add_worktree(worktree, branch, "origin/main")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.save_approval(
        1, body=BODY, body_sha=body_hash(BODY),
        acceptance_blobs=git.blob_shas("origin/main", [ACCEPTANCE]),
        approved_by="human", approved_at=BASE_TIME.isoformat(),
    )
    store.start_run(1, branch=branch, worktree=worktree)
    tracker = FakeTracker(now=lambda: BASE_TIME, strict=True)
    tracker.add_issue(1, "Feature", BODY, labels=["approved"])
    ctx = RunContext(
        repo="owner/repo", repo_root=git.root, git=git, store=store, tracker=tracker,
        config=Config(review="fast", reviewer="none"), now=lambda: BASE_TIME,
    )
    return SimpleNamespace(
        n=1, ctx=ctx, git=git.at(worktree), worktree=worktree, branch=branch,
        start_ref="origin/main", target_branch="main", log=store.log_path(1),
        approval=store.load_approval(1), history=git_history, paths=[],
    )


def test_fetch_retains_an_already_verified_base(replay_case):
    attempt = replay_case
    head = attempt.git.head_sha()
    captured = fetch_move(attempt, head)
    attempt.history.update({"incompatible.txt": "Later base\n"})
    attempt.ctx.git.fetch("origin", "main")
    assert captured == BaseMove(head, head)
    assert captured.target_sha != attempt.git.ref_sha("origin/main")


def test_rebuild_recreates_the_base_that_passed_acceptance(replay_case, monkeypatch):
    attempt = replay_case
    attempt.history.update({"checked.txt": "Checked base\n"})
    checked = attempt.history.tip
    validate = run_conflicts._acceptance_matches

    def validate_then_fetch(*args, **kwargs):
        valid = validate(*args, **kwargs)
        assert valid
        attempt.history.update({ACCEPTANCE: "Unapproved acceptance\n", "unchecked.txt": "Later base\n"})
        attempt.ctx.git.fetch("origin", "main")
        return valid

    monkeypatch.setattr(run_conflicts, "_acceptance_matches", validate_then_fetch)
    run_conflicts._rebuild(attempt, ConflictBudgetExhausted("conflict budget"))
    assert attempt.start_ref == attempt.git.head_sha() == checked
    assert (attempt.worktree / "checked.txt").is_file()
    assert not (attempt.worktree / "unchecked.txt").exists()
    assert attempt.git.blob_shas(checked, [ACCEPTANCE]) == attempt.approval.acceptance_blobs


@pytest.mark.parametrize("when", ["remove_worktree", "add_worktree"])
def test_recreation_uses_one_base_for_head_and_skills(replay_case, monkeypatch, when):
    attempt = replay_case
    checked = attempt.git.ref_sha(attempt.start_ref)
    operation = getattr(attempt.ctx.git, when)
    inject = run_conflicts._inject_lane_skills
    injected = []

    def recreate_then_fetch(*args, **kwargs):
        result = operation(*args, **kwargs)
        attempt.history.update({"unchecked.txt": "Later base\n"})
        attempt.ctx.git.fetch("origin", "main")
        return result

    def inject_at_base(ctx, number, git, target_ref):
        injected.append(target_ref)
        return inject(ctx, number, git, target_ref)

    monkeypatch.setattr(attempt.ctx.git, when, recreate_then_fetch)
    monkeypatch.setattr(run_conflicts, "_inject_lane_skills", inject_at_base)
    run_conflicts._recreate_worktree(attempt)
    assert attempt.start_ref == attempt.git.head_sha() == checked
    assert injected == [checked]
    assert not (attempt.worktree / "unchecked.txt").exists()


@pytest.mark.parametrize("pinned", [False, True])
def test_resume_merges_the_base_captured_before_lock_wait(replay_case, monkeypatch, pinned):
    attempt = replay_case
    if pinned:
        attempt.start_ref = attempt.git.ref_sha(attempt.start_ref)
    attempt.history.update({"checked.txt": "Captured before lock wait\n"})
    checked = attempt.history.tip
    attempt.ctx.git.fetch("origin", "main")

    @contextmanager
    def wait_then_fetch(number, ctx):
        attempt.history.update({"unchecked.txt": "Later base\n"})
        ctx.git.fetch("origin", "main")
        yield

    monkeypatch.setattr(run_publish, "_merge_lock", wait_then_fetch)
    assert not run_conflicts._resume_implementation(attempt)
    assert attempt.start_ref == attempt.git.head_sha() == checked
    assert not (attempt.worktree / "unchecked.txt").exists()


@pytest.mark.parametrize("batch", [False, True])
def test_final_inputs_check_the_captured_base_when_tracking_refs_move(replay_case, monkeypatch, batch):
    attempt = replay_case
    ctx = attempt.ctx
    base = "foragent/batch-99" if batch else "main"
    checked = attempt.git.head_sha()
    if batch:
        ctx.tracker.add_issue(99, "Parent")
        ctx.tracker.set_parent(1, 99)
        git_command(attempt.history.origin, "update-ref", f"refs/heads/{base}", checked)
        ctx.git.fetch("origin", base)
    pr = ctx.tracker.create_pr(head=attempt.branch, base=base, title="Feature", body="")
    ctx.tracker.set_pr_head_sha(pr, checked)
    ctx.tracker.set_pr_files(pr, ["feature.txt"])
    ctx.tracker.set_pr_checks(pr, "pass")
    resolve = attempt.git.ref_sha
    changed_paths = attempt.git.changed_paths
    paths_bases = []
    raced = []

    def capture_then_fetch(ref):
        sha = resolve(ref)
        if ref == "origin/main" and not raced:
            raced.append(sha)
            attempt.history.update({ACCEPTANCE: "Unapproved acceptance\n"})
            ctx.git.fetch("origin", "main")
            if batch:
                git_command(attempt.history.origin, "update-ref", f"refs/heads/{base}", attempt.history.tip)
                ctx.git.fetch("origin", base)
        return sha

    def paths_at_base(ref):
        paths_bases.append(ref)
        return changed_paths(ref)

    monkeypatch.setattr(attempt.git, "ref_sha", capture_then_fetch)
    monkeypatch.setattr(attempt.git, "changed_paths", paths_at_base)
    inputs = run_publish._final_merge_inputs(
        1, ctx, attempt.git, pr, attempt.branch, base, checked, attempt.approval, BASE_TIME.timestamp(),
    )
    assert raced == [checked]
    assert inputs.stop is None
    assert inputs.base_sha == checked
    assert paths_bases == [checked]
    assert inputs.paths == ["feature.txt"]

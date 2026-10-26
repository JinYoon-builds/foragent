"""WIP/removal races use real Git trees and flock descriptors, never worker processes."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import fcntl
import json
import threading

import pytest

from foragent.run_publish import _preserve_failed_implementation
from foragent.scheduler import TickContext, TickReport, _remove_run_worktree

from conftest import git_command
from runner_support import import_index, run_case


@pytest.fixture
def wip_case(run_case, request):
    c = run_case
    c.history.update({**{f"src/file-{i:02}.txt": "tracked\n" for i in range(12)},
                      "src/[ab].txt": "literal brackets\n", "src/a.txt": "other file\n",
                      **getattr(request, "param", {})})
    c.ctx.git.fetch("origin", "main")
    c.path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(c.path, "foragent/issue-1", "origin/main")
    c.git = c.ctx.git.at(c.path)
    yield c
    c.ctx.git.remove_worktree(c.path)
    c.store.finish_run(1, "killed")
    c.tracker.remove_labels(1, ["running"])


def preserve(c, **kwargs):
    _preserve_failed_implementation(
        1, c.ctx, c.git, "stalled-restart", approval=c.store.load_approval(1), **kwargs,
    )


def events(c):
    path = c.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("operation", ["preserve", "remove"])
def test_wip_and_removal_wait_for_another_process_lock(wip_case, operation):
    c = wip_case
    (c.path / "unfinished.txt").write_text("partial work\n")
    before = c.git.head_sha()
    lock = c.store.root / "runs/issue-1.worktree.lock"
    started = threading.Event()

    def action():
        started.set()
        if operation == "preserve":
            preserve(c, tracker_available=False)
        else:
            c.ctx.git.remove_worktree(c.path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock.open("a") as other_process:
            fcntl.flock(other_process, fcntl.LOCK_EX)
            try:
                future = pool.submit(action)
                assert started.wait(5)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
                assert c.path.is_dir() and c.git.head_sha() == before
                # A different issue does not share this lock.
                with c.store.worktree_lock(2):
                    pass
            finally:
                fcntl.flock(other_process, fcntl.LOCK_UN)
        future.result(timeout=10)
    if operation == "preserve":
        assert c.git.head_sha() != before
    else:
        assert not c.path.exists()
        assert git_command(c.history.clone, "rev-parse", "foragent/issue-1") == before


@pytest.mark.parametrize("operation", ["preserve", "remove"])
def test_preservation_keeps_lock_through_push_decision(wip_case, operation):
    c = wip_case
    (c.path / "unfinished.txt").write_text("partial work\n")
    pushing, release, started = threading.Event(), threading.Event(), threading.Event()

    def push():
        pushing.set()
        assert release.wait(10)

    def competing_action():
        started.set()
        if operation == "preserve":
            preserve(c, tracker_available=False)
        else:
            c.ctx.git.remove_worktree(c.path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(preserve, c, push_branch=push)
        try:
            assert pushing.wait(5)
            count = len(c.operations)
            second = pool.submit(competing_action)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
            assert c.path.is_dir() and len(c.operations) == count
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert git_command(c.history.clone, "rev-list", "--count", "origin/main..foragent/issue-1") == "1"


def test_preservation_rechecks_worktree_after_waiting_for_removal(wip_case):
    c = wip_case
    (c.path / "unfinished.txt").write_text("partial work\n")
    before = c.git.head_sha()
    started = threading.Event()

    def waiting_preservation():
        started.set()
        preserve(c)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with (c.store.root / "runs/issue-1.worktree.lock").open("a") as removing_process:
            fcntl.flock(removing_process, fcntl.LOCK_EX)
            try:
                future = pool.submit(waiting_preservation)
                assert started.wait(5)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
                # Simulate the lock holder's removal, bypassing a second lock acquisition.
                c.ctx.git._call(["worktree", "remove", "--force", str(c.path)])
            finally:
                fcntl.flock(removing_process, fcntl.LOCK_UN)
        future.result(timeout=10)
    assert git_command(c.history.clone, "rev-parse", "foragent/issue-1") == before
    assert not any("commit" in args or args[:1] == ["push"] for _, args in c.operations)


@pytest.mark.parametrize("invalid", ["missing-directory", "missing-git-file", "invalid-git-file"])
def test_preservation_skips_invalid_worktree_without_staging(wip_case, invalid):
    c = wip_case
    before = c.git.head_sha()
    c.ctx.git.remove_worktree(c.path)
    if invalid != "missing-directory":
        c.path.mkdir()
        if invalid == "invalid-git-file":
            (c.path / ".git").write_text("gitdir: missing\n")
    count = len(c.operations)
    preserve(c)
    assert git_command(c.history.clone, "rev-parse", "foragent/issue-1") == before
    assert not any({"add", "commit", "push"}.intersection(args) for _, args in c.operations[count:])
    # These invalid directories are not registered Git worktrees.
    if c.path.exists():
        (c.path / ".git").unlink(missing_ok=True)
        c.path.rmdir()


def test_untouched_mass_deletion_is_left_out_while_other_work_is_committed(wip_case):
    c = wip_case
    deleted = [f"src/file-{i:02}.txt" for i in range(12)]
    for path in deleted:
        (c.path / path).unlink()
    c.store.update_run(1, changed_files=["src/a.txt"])
    (c.path / "src/a.txt").unlink()
    (c.path / "src/[ab].txt").write_text("edited\n")
    (c.path / "unfinished.txt").write_text("keep this work\n")
    c.git.stage_changes()
    before = c.git.head_sha()
    preserve(c)
    assert c.git.head_sha() != before and not c.git.has_staged_changes()
    assert all(not (c.path / path).exists() for path in deleted)
    assert (c.path / "unfinished.txt").read_text() == "keep this work\n"
    tracked = git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert set(deleted) <= set(tracked)
    assert "src/a.txt" not in tracked
    assert git_command(c.path, "show", "HEAD:unfinished.txt") == "keep this work"
    assert git_command(c.path, "show", "HEAD:src/[ab].txt") == "edited"
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.git.head_sha()
    refused, = [event for event in events(c) if event["kind"] == "wip-preservation-refused"]
    assert refused["issue"] == 1
    assert all(path in refused["summary"] for path in deleted[:10])
    assert all(path not in refused["summary"] for path in deleted[10:])
    assert "WIP preservation refused" in c.store.log_path(1).read_text()


@pytest.mark.parametrize("fields", [
    {"declared_files": ["src/**"]},
    {"effective_files": ["src/**"], "files_committed": False},
    {"effective_files": ["src/file-00.txt"], "files_committed": True},
    {"changed_files": ["src/file-00.txt"]},
    {"late_files": ["src/file-00.txt"]},
])
def test_recorded_deletion_is_still_committed(wip_case, fields):
    c = wip_case
    c.store.update_run(1, **fields)
    (c.path / "src/file-00.txt").unlink()
    before = c.git.head_sha()
    preserve(c, tracker_available=False)
    assert c.git.head_sha() != before
    assert "src/file-00.txt" not in git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD")
    assert (c.path / "src/file-01.txt").read_text() == "tracked\n"


@pytest.mark.parametrize("field", ["effective_files", "changed_files", "late_files"])
@pytest.mark.parametrize("deleted,allowed", [("src/[ab].txt", True), ("src/a.txt", False)])
def test_measured_files_with_glob_characters_are_literal(wip_case, field, deleted, allowed):
    c = wip_case
    c.store.update_run(1, **{field: ["src/[ab].txt"], "files_committed": True})
    # Nine other unrecorded deletions: the tenth decides whether this is a refused mass deletion.
    for i in range(9):
        (c.path / f"src/file-{i:02}.txt").unlink()
    (c.path / deleted).unlink()
    before = c.git.head_sha()
    if allowed:
        preserve(c, tracker_available=False)
        assert c.git.head_sha() != before
    else:
        preserve(c, tracker_available=False)
        assert c.git.head_sha() == before
        assert not c.git.has_staged_changes()
        assert deleted in events(c)[-1]["summary"]


@pytest.mark.parametrize("path", ["src/branch-added.txt", "src/[ab].txt"], ids=["added", "modified"])
def test_branch_owned_file_deleted_later_is_committed(wip_case, path):
    c = wip_case
    file = c.path / path
    file.write_text("branch work\n")
    c.git.stage_changes()
    import_index(c.path, "Own branch change")
    before = c.git.head_sha()
    file.unlink()

    preserve(c, tracker_available=False)

    assert c.git.head_sha() != before
    assert path not in git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not events(c)


@pytest.mark.parametrize("wip_case,skill_path", [
    ({".foragent/acceptance-exclusions.json": "[]\n"}, None),
    ({".claude/skills/helper[1]/SKILL.md": "injected skill\n"}, ".claude/skills/helper[1]"),
], indirect=["wip_case"], ids=["protocol", "local-skill"])
def test_runner_artifact_removal_is_not_refused(wip_case, skill_path):
    c = wip_case
    if skill_path:
        c.store.update_run(1, local_skill_paths=[skill_path])
    artifact = f"{skill_path}/SKILL.md" if skill_path else ".foragent/acceptance-exclusions.json"
    before = c.git.head_sha()

    preserve(c, tracker_available=False)

    assert c.git.head_sha() != before
    assert artifact not in git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert (c.path / artifact).is_file()
    assert not events(c)


def test_branch_own_deletion_history_allows_deletion_without_recorded_files(wip_case):
    c = wip_case
    paths = [c.path / f"src/file-{i:02}.txt" for i in range(2)]
    for path in paths:
        path.unlink()
        c.git.stage_changes()
        import_index(c.path, "Delete own file")
    for path in paths:
        path.write_text("restored\n")
    c.git.stage_changes()
    import_index(c.path, "Restore own files")
    before = c.git.head_sha()
    for path in paths:
        path.unlink()
    preserve(c, tracker_available=False)
    assert c.git.head_sha() != before
    tracked = git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD")
    assert all(path.relative_to(c.path).as_posix() not in tracked for path in paths)


def test_a_few_unrecorded_deletions_are_kept_as_the_implementers_work(wip_case):
    """The locked #270 check: an implementer's single deletion belongs in the WIP."""
    c = wip_case
    (c.path / "src/file-00.txt").unlink()
    preserve(c, tracker_available=False)
    assert "src/file-00.txt" not in git_command(c.path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not [event for event in events(c) if event["kind"] == "wip-preservation-refused"]


def test_target_branch_deletion_does_not_authorize_untouched_wip_deletion(wip_case):
    c = wip_case
    c.history.update({"src/file-00.txt": None})
    c.ctx.git.fetch("origin", "main")
    # A mass of unrecorded deletions, one of them also deleted on the target branch.
    for i in range(10):
        (c.path / f"src/file-{i:02}.txt").unlink()
    before = c.git.head_sha()
    preserve(c, tracker_available=False)
    assert c.git.head_sha() == before
    assert "src/file-00.txt" in events(c)[-1]["summary"]


def test_recovery_preserves_other_work_and_does_not_report_refusal_as_failure(wip_case):
    c = wip_case
    before = c.git.head_sha()
    for i in range(10):
        (c.path / f"src/file-{i:02}.txt").unlink()
    (c.path / "unfinished.txt").write_text("keep this work\n")
    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
    )
    report = TickReport()
    _remove_run_worktree(ctx, 1, c.path, report, outcome="stalled-restart")
    assert not c.path.exists()
    assert git_command(c.history.clone, "rev-parse", "foragent/issue-1") != before
    assert git_command(c.history.clone, "show", "foragent/issue-1:unfinished.txt") == "keep this work"
    assert git_command(c.history.clone, "show", "foragent/issue-1:src/file-00.txt") == "tracked"
    kinds = {event["kind"] for event in events(c)}
    assert "wip-preservation-refused" in kinds and "wip-preservation-failed" not in kinds
    assert not any("WIP preservation failed" in action for action in report.actions)


def test_lock_order_allows_state_updates_under_tick_and_worktree_locks(wip_case):
    c = wip_case
    with c.store.tick_lock() as acquired:
        assert acquired
        with c.store.worktree_lock(1):
            c.store.update_run(1, phase="preserving")
    assert c.store.get_run(1).phase == "preserving"

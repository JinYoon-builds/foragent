from dataclasses import replace
from datetime import timedelta
import fcntl
import json
import shlex
import sys

import pytest

from foragent.git import GitError, GitRepo
from foragent.issue import body_hash, parse_body
from foragent.reasons import Reason
from foragent.runner import run_issue
from foragent.state import StateStore

from conftest import ACCEPTANCE, git_command
from runner_support import (
    COMMAND, batch_setup, conflicting_lanes, enable_reviewer, run_case, seed_batch, set_done_when,
)


@pytest.mark.parametrize("saved_at", ["local", "origin"])
def test_retry_integrates_latest_target_before_worker_and_baseline_verification(run_case, monkeypatch, saved_at):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0, test_command=f"{shlex.quote(sys.executable)} -m pytest")
    c.executor.steps = [{"feature.txt": "unfinished"}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    if saved_at == "origin":
        git_command(c.history.clone, "update-ref", "-d", "refs/heads/foragent/issue-1")
    sibling = "tests/acceptance/test_sibling.py"
    c.history.update({sibling: "def test_sibling():\n    assert True\n"})
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.executor.steps = [{"feature.txt": "done"}]
    execute = c.executor.run

    def finish(**kwargs):
        root = kwargs["worktree"]
        assert (root / "feature.txt").read_text() == "unfinished"
        assert (root / sibling).is_file(), "latest target must be present before the worker runs"
        assert GitRepo(root).is_ancestor(c.history.tip, git_command(root, "rev-parse", "HEAD"))
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", finish)
    assert run_issue(1, c.ctx) == "merged"
    assert "test_sibling" in c.store.log_path(1).read_text()


def test_retry_target_conflict_uses_existing_resolver_before_ordinary_implementation(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.history.update({"cli.py": "commands = ['help']\n"})
    c.executor.steps = [{"feature.txt": "unfinished", "cli.py": "commands = ['help', 'issue']\n"}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    c.history.update({"cli.py": "commands = ['help', 'base']\n", "sibling.txt": "new target file\n"})
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.executor.steps = [{"feature.txt": "done", "cli.py": "commands = ['help', 'base', 'issue']\n"}]
    execute = c.executor.run
    calls = []

    def resolve(**kwargs):
        root = kwargs["worktree"]
        assert c.store.get_run(1).phase == "resolving-conflict"
        assert (root / "feature.txt").read_text() == "unfinished"
        assert (root / "sibling.txt").is_file()
        assert GitRepo(root).conflict_paths() == ["cli.py"]
        assert "Resolve the conflict markers" in kwargs["prompt"]
        calls.append(kwargs)
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", resolve)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1


@pytest.mark.parametrize("saved_at", ["local", "origin"])
@pytest.mark.parametrize("conflicting", [False, True])
def test_retry_binds_reapproved_acceptance_after_integrating_target(run_case, monkeypatch, saved_at, conflicting):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.history.update({"notes.txt": "original\n"})
    c.executor.steps = [{"feature.txt": "unfinished", "notes.txt": "issue work\n"}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    if saved_at == "origin":
        git_command(c.history.clone, "update-ref", "-d", "refs/heads/foragent/issue-1")
    acceptance = "def test_feature():\n    assert 1 + 1 == 2\n"
    c.history.update({ACCEPTANCE: acceptance, **({"notes.txt": "base work\n"} if conflicting else {})})
    approval = c.store.load_approval(1)
    c.store.save_approval(
        1, body=approval.body, body_sha=approval.body_sha,
        acceptance_blobs=GitRepo(c.history.origin).blob_shas("main", [ACCEPTANCE]),
        approved_by=approval.approved_by, approved_at=c.now[0].isoformat(),
    )
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.executor.steps = [{"feature.txt": "done", "notes.txt": "base work\nissue work\n"}]
    execute = c.executor.run

    def finish(**kwargs):
        assert (kwargs["worktree"] / ACCEPTANCE).read_text() == acceptance
        assert (kwargs["worktree"] / "feature.txt").read_text() == "unfinished"
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", finish)
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.load_approval(1) is not None
    assert git_command(c.history.origin, "rev-parse", f"foragent/issue-1:{ACCEPTANCE}") == (
        c.store.load_approval(1).acceptance_blobs[ACCEPTANCE]
    )


def test_path_override_replays_child_changes_on_batch_before_checks(run_case):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    seed_batch(c, {"sibling.txt": "previous child"})
    set_done_when(c, [COMMAND, "test -f sibling.txt"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "foragent/batch-99"


@pytest.mark.parametrize("conflicting", [False, True])
def test_rebuttal_survives_batch_retarget_and_patch_recovery(run_case, conflicting):
    c = run_case
    c.history.update({"shared.txt": "base\n"})
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"shared.txt": "sibling\n"} if conflicting else {"sibling.txt": "sibling\n"})
    rebuttal = "- payment support: outside the approved goal\n"
    c.executor.steps[0].update({"shared.txt": "child\n", ".foragent/REBUTTAL.md": rebuttal})
    if conflicting:
        c.executor.steps.append({"feature.txt": "done", "shared.txt": "resolved\n"})
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == (2 if conflicting else 1)
    assert "Implementer rebuttals (judge them):\n" + rebuttal in calls[0]["prompt"]
    assert rebuttal in c.tracker.created_prs[0].body
    assert ".foragent/REBUTTAL.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


@pytest.mark.parametrize("conflicting", [False, True])
def test_batch_patch_uses_three_way_merge_and_retries_real_conflicts(run_case, conflicting):
    c = run_case
    original = [f"line {i}\n" for i in range(1, 11)]
    c.history.update({"shared.txt": "".join(original)})
    sibling = original.copy()
    sibling[4] = "sibling change\n"
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"shared.txt": "".join(sibling)})
    child = original.copy()
    child[4 if conflicting else 2] = "child change\n"
    c.executor.steps = [{"feature.txt": "done", "shared.txt": "".join(child)}]
    if conflicting:
        resolved = sibling.copy()
        resolved[2] = "child change\n"
        c.executor.steps.append({"feature.txt": "done", "shared.txt": "".join(resolved)})
    assert run_issue(1, c.ctx) == "merged"
    shared = git_command(c.history.origin, "show", "foragent/issue-1:shared.txt").splitlines()
    assert shared[2] == "child change" and shared[4] == "sibling change"
    # A replay conflict is resolved in place (#416); it does not spend an implementation attempt.
    assert c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).rebuild_count == 0
    assert c.store.load_flag(1) is None
    assert any(args[:2] == ["apply", "--3way"] for _, args in c.operations)
    if conflicting:
        assert "Could not apply child changes" in c.executor.calls[1]["prompt"]
        assert "shared.txt" in c.executor.calls[1]["prompt"]


@pytest.mark.parametrize("before_push", [False, True])
@pytest.mark.parametrize("retries", [0, 1])
def test_fresh_base_generated_batch_path_replay_conflict_uses_remaining_attempts(run_case, monkeypatch, before_push, retries):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"switch.txt": "batch"}, max_retries=retries)
    seed_batch(c, {"switch.txt": "sibling\n", "sibling.txt": "keep\n"})
    set_done_when(c, [COMMAND, "if test -f fresh.txt && ! test -f switch.txt; then echo child > switch.txt; fi"])
    c.executor.steps += [{"feature.txt": "done", "switch.txt": "resolved\n"}]
    moved = []
    execute = c.executor.run

    def move_base():
        c.history.update({"fresh.txt": "generate a batch path\n"})
        moved.append(c.history.tip)

    def implement(**kwargs):
        if c.executor.calls:
            # The replay conflict is resolved in place on the destination (#416): the child's work stays.
            root = kwargs["worktree"]
            assert "<<<<<<<" in (root / "switch.txt").read_text()
            assert (root / "feature.txt").exists()
        result = execute(**kwargs)
        if before_push and len(c.executor.calls) == 1:
            move_base()
        return result

    def ci(pr):
        if not moved:
            move_base()
        return "pass"

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    # In-place resolution uses the conflict budget, not implementation attempts, so both budgets merge.
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 1
    assert "Could not apply child changes to foragent/batch-99" in c.store.log_path(1).read_text()
    assert "Could not apply child changes to foragent/batch-99" in c.executor.calls[1]["prompt"]
    assert c.tracker.created_prs[0].base == "foragent/batch-99"
    assert git_command(c.history.origin, "show", "foragent/issue-1:switch.txt") == "resolved"
    assert git_command(c.history.origin, "show", "foragent/issue-1:sibling.txt") == "keep"
    assert c.store.load_flag(1) is None


def test_unchanged_base_does_not_merge_or_repeat_checks(run_case):
    c = run_case
    assert run_issue(1, c.ctx) == "merged"
    assert not any("merge" in args for _, args in c.operations)
    assert c.store.log_path(1).read_text().count("Done when command:") == 2


@pytest.mark.parametrize("batch", [False, True])
def test_clean_fresh_base_rechecks_ci_and_merges_new_sha_without_review(run_case, monkeypatch, batch):
    c = run_case
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    if batch:
        batch_setup(c)
    heads = []

    def checks(pr):
        heads.append(c.tracker.pr_head_sha(pr))
        if len(heads) == 1:
            if batch:
                seed_batch(c, {"parallel.txt": "fresh"})
            else:
                c.history.update({"parallel.txt": "fresh"})
        head = heads[-1]
        return "pending" if head != heads[0] and heads.count(head) == 1 else "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1 and len(c.executor.calls) == 1
    assert len(heads) == 4 and heads[0] != heads[1] == heads[2] == heads[3]
    assert c.tracker.pr_head_sha(c.tracker.merged_prs[0]) == heads[-1]
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    assert git_command(c.history.origin, "show", "foragent/issue-1:parallel.txt") == "fresh"
    target = "foragent/batch-99" if batch else "main"
    target_sha = git_command(c.history.origin, "rev-parse", target)
    assert any(comment.body == f"base merged before merge: {target_sha}"
               for comment in c.tracker.list_comments(c.tracker.merged_prs[0]))


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("before_push", [False, True])
def test_fresh_base_same_line_conflict_is_resolved_in_place_and_merge_commit_is_verified(conflicting_lanes, monkeypatch, executor, before_push):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, executor=executor)
    c.executor.steps.append({"cli.py": "commands = ['help', 'base', 'issue']\n"})
    implement = c.executor.run
    merge = c.tracker.merge_pr
    implementation_heads = []

    def resolve(**kwargs):
        initial = not c.executor.calls
        assert c.store.get_run(1).phase == ("implementing" if initial else "resolving-conflict")
        if not initial:
            root = kwargs["worktree"]
            text = (root / "cli.py").read_text()
            assert all(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>"))
            assert "['help', 'issue']" in text and "['help', 'base']" in text
            assert git_command(root, "rev-parse", "MERGE_HEAD") == c.target_heads[0]
            assert GitRepo(root).conflict_paths() == ["cli.py"]
            assert c.store.get_run(1).attempts == 1
            implementation_heads.append(git_command(root, "rev-parse", "HEAD"))
            # Conflict resolution releases the final lock before calling the implementer.
            with (c.store.root / "merge.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        result = implement(**kwargs)
        if initial and before_push:
            c.move_target()
        return result

    def merge_verified(pr, *, method, expected_head_sha):
        root = c.store.worktree_path(1)
        assert c.store.get_run(1).phase == "merging"
        assert expected_head_sha == git_command(root, "rev-parse", "HEAD")
        assert not (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists()
        parents = git_command(root, "show", "-s", "--format=%P", "HEAD").split()
        assert parents == [implementation_heads[0], c.target_heads[0]]
        merge(pr, method=method, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(c.executor, "run", resolve)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_verified)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 1
    prompt = c.executor.calls[1]["prompt"]
    assert "Resolve the conflict markers in the following files:\n- cli.py" in prompt
    assert "Preserve both the features newly added on the base branch and the features for this issue." in prompt
    assert "Do not commit. The runner will stage the resolution and create the merge commit." in prompt
    if before_push:
        assert c.ci_heads == [c.tracker.pr_head_sha(2)] * 2
    else:
        assert c.ci_heads == [c.ci_heads[0]] + [c.tracker.pr_head_sha(2)] * 3
    assert "attempt 1" in c.tracker.created_prs[0].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 2
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == "commands = ['help', 'base', 'issue']"
    assert not any(args == ["merge", "--abort"] for _, args in c.operations)
    if executor == "claude":
        settings = json.loads(c.executor.calls[1]["settings_path"].read_text())
        assert "Bash(git merge:*)" in settings["permissions"]["deny"]


@pytest.mark.parametrize("width", [5, 7, 12])
@pytest.mark.parametrize("marker", ["<", "=", "|", ">"])
def test_conflict_marker_left_after_staging_consumes_attempt_before_merge_commit(conflicting_lanes, monkeypatch, width, marker):
    c = conflicting_lanes
    if width != 7:
        c.history.update({".gitattributes": f"cli.py conflict-marker-size={width}\n"})
    git_command(c.history.clone, "config", "merge.conflictStyle", "diff3")
    c.executor.steps += [
        {"cli.py": f"commands = ['help', 'base', 'issue']\n{'<' * width} HEAD\n{marker * width}\n{'>' * width} target\n"},
        {"cli.py": "commands = ['help', 'base', 'issue']\n"},
    ]
    implement = c.executor.run

    def prestage(**kwargs):
        if len(c.executor.calls) == 1:
            content = (kwargs["worktree"] / "cli.py").read_text()
            assert f"{'<' * width} HEAD\n" in content and f"{'|' * width} " in content
        result = implement(**kwargs)
        if len(c.executor.calls) == 2:
            git_command(kwargs["worktree"], "add", "cli.py")
        if len(c.executor.calls) == 3:
            assert git_command(kwargs["worktree"], "rev-parse", "MERGE_HEAD") == c.target_heads[0]
        return result

    monkeypatch.setattr(c.executor, "run", prestage)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 1
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert "Resolve the conflict markers" in c.executor.calls[2]["prompt"]
    assert c.store.log_path(1).read_text().count("Done when command:") == 2
    assert len(git_command(c.history.origin, "show", "-s", "--format=%P", "foragent/issue-1").split()) == 2


@pytest.mark.parametrize("width", [5, 12])
def test_noop_conflict_resolver_is_retried_even_when_done_when_checks_other_files(conflicting_lanes, width):
    c = conflicting_lanes
    c.history.update({".gitattributes": f"cli.py conflict-marker-size={width}\n"})
    set_done_when(c, [f"test -f {ACCEPTANCE}"])
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 1
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == "commands = ['help', 'base', 'issue']"


def test_done_when_reintroduced_ancestor_marker_in_conflict_file_consumes_attempt(conflicting_lanes):
    c = conflicting_lanes
    set_done_when(c, [
        f"test -f {ACCEPTANCE}",
        "if test -f base-feature.txt && ! test -f fixed.txt; then printf '<<<<<<< HEAD\\n||||||| ancestor\\n=======\\n>>>>>>> target\\n' > cli.py; fi",
    ])
    resolved = "commands = ['help', 'base', 'issue']\n"
    c.executor.steps += [{"cli.py": resolved}, {"cli.py": resolved, "fixed.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 1
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == resolved.strip()


def test_conflict_resolution_done_when_failure_uses_remaining_attempt_on_merged_base(conflicting_lanes, monkeypatch):
    c = conflicting_lanes
    c.executor.steps += [
        {"cli.py": "commands = ['help', 'issue']\n"},
        {"cli.py": "commands = ['help', 'base', 'issue']\n"},
    ]
    implement = c.executor.run

    def fix_checks(**kwargs):
        if len(c.executor.calls) == 2:
            root = kwargs["worktree"]
            assert (root / "base-feature.txt").exists()
            assert GitRepo(root).is_ancestor(c.target_heads[0], git_command(root, "rev-parse", "HEAD"))
            assert not (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists()
        return implement(**kwargs)

    monkeypatch.setattr(c.executor, "run", fix_checks)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 1
    assert "Command:" in c.executor.calls[2]["prompt"] and "Resolve the conflict markers" in c.executor.calls[2]["prompt"]
    assert "attempt 1" in c.tracker.created_prs[0].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 3
    assert c.ci_heads == [c.ci_heads[0]] + [c.tracker.pr_head_sha(2)] * 3


@pytest.mark.parametrize("source", ["implementation", "resolution", "checks"])
def test_conflict_resolution_ignores_section_underlines_in_other_changed_files(conflicting_lanes, source):
    c = conflicting_lanes
    readme = "Heading\n=======\n"
    resolution = {"cli.py": "commands = ['help', 'base', 'issue']\n"}
    if source == "implementation":
        c.executor.steps[0]["README.rst"] = readme
    elif source == "resolution":
        resolution["README.rst"] = readme
    else:
        commands = parse_body(c.store.load_approval(1).body).done_when_commands
        set_done_when(c, [*commands, "printf 'Heading\\n=======\\n' > README.rst"])
    c.executor.steps.append(resolution)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 1
    assert git_command(c.history.origin, "show", "foragent/issue-1:README.rst") == readme.strip()


@pytest.mark.parametrize("failure", ["markers", "checks"])
def test_conflict_resolution_exhaustion_never_publishes_failed_resolution(conflicting_lanes, failure):
    c = conflicting_lanes
    content = "commands = ['help', 'issue']\n" + ("<<<<<<< HEAD\n=======\n>>>>>>> target\n" if failure == "markers" else "")
    c.executor.steps += [{"cli.py": content}] * 3
    assert run_issue(1, c.ctx) == "retries-exceeded"
    if failure == "markers":
        # The one own file still conflicts after the in-place budget: one rebuild, which also fails.
        assert len(c.executor.calls) == 4 and c.store.get_run(1).attempts == 2
        assert c.store.get_run(1).rebuild_count == 1
    else:
        # Markers are gone but checks fail: no file still conflicts, so no rebuild (#416).
        assert len(c.executor.calls) == 1 + c.ctx.config.conflict_retries
        assert c.store.get_run(1).attempts == 1 and c.store.get_run(1).rebuild_count == 0
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert len(c.ci_heads) == 1 and c.tracker.merged_prs == []
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.ci_heads[0]
    assert not any(args == ["merge", "--abort"] for _, args in c.operations)


@pytest.mark.parametrize("failure", ["markers", "checks"])
def test_failed_conflict_resolution_resumes_local_wip_while_preserving_open_pr(conflicting_lanes, monkeypatch, failure):
    c = conflicting_lanes
    branch = "foragent/issue-1"
    content = "commands = ['help', 'issue']\n" + ("<<<<<<< HEAD\n=======\n>>>>>>> target\n" if failure == "markers" else "")
    c.executor.steps += [{"cli.py": content}] * 3
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert git_command(c.history.clone, "show", f"{branch}:cli.py") == content.strip()
    if failure == "markers":
        assert "[retries-exceeded]" in git_command(c.history.clone, "log", "-1", "--format=%B", branch)
        assert git_command(c.history.clone, "rev-parse", f"{branch}^") == c.target_heads[0]
    else:
        # No rebuild (#416): the in-place resolution's merge commit is kept on the local branch.
        parents = git_command(c.history.clone, "show", "-s", "--format=%P", branch).split()
        assert len(parents) == 2 and c.target_heads[-1] in parents
    assert git_command(c.history.origin, "rev-parse", branch) == c.ci_heads[0]
    assert git_command(c.history.origin, "rev-parse", "main") == c.history.tip

    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch=branch, worktree=c.store.worktree_path(1))
    c.executor.steps = [{"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    execute = c.executor.run

    def finish(**kwargs):
        assert (kwargs["worktree"] / "cli.py").read_text() == content
        assert git_command(c.history.origin, "rev-parse", branch) == c.ci_heads[0]
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", finish)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1 and c.tracker.merged_prs == [2]
    assert git_command(c.history.origin, "show", f"{branch}:cli.py") == "commands = ['help', 'base', 'issue']"


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("restart", ["resolve", "noop"])
def test_interrupted_conflict_keeps_single_parent_wip_and_rechecks_on_restart(conflicting_lanes, monkeypatch, staged, restart):
    c = conflicting_lanes
    set_done_when(c, ["test -f feature.txt"])
    c.executor.steps = [{"feature.txt": "done", "cli.py": "commands = ['help', 'issue']\n"}, {}]
    execute = c.executor.run

    def interrupt(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 1:
            c.move_target()
        else:
            (kwargs["worktree"] / "partial.txt").write_text("keep partial resolution\n")
            if staged:
                git_command(kwargs["worktree"], "add", "-A")
            return replace(result, timed_out=True)
        return result

    monkeypatch.setattr(c.executor, "run", interrupt)
    assert run_issue(1, c.ctx) == "timeout"
    branch = "foragent/issue-1"
    saved = git_command(c.history.clone, "rev-parse", branch)
    assert len(git_command(c.history.clone, "show", "-s", "--format=%P", saved).split()) == 1
    assert c.store.get_run(1).merge_conflicts == ["cli.py"]
    assert "<<<<<<<" in git_command(c.history.clone, "show", f"{saved}:cli.py")
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch=branch, worktree=c.store.worktree_path(1))
    assert c.store.get_run(1).merge_conflicts == ["cli.py"]
    resolution = {"cli.py": "commands = ['help', 'base', 'issue']\n", "feature.txt": "done"}
    c.executor.steps = [resolution] if restart == "resolve" else [{}, {}, resolution]
    seen = []

    def resume(**kwargs):
        if not seen:
            assert "Resolve the conflict markers" in kwargs["prompt"]
            assert (kwargs["worktree"] / "partial.txt").read_text() == "keep partial resolution\n"
        seen.append(kwargs)
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", resume)
    if restart == "noop":
        # One of the PR's three own files conflicts: keep the work and stop for the orchestrator (#416).
        assert run_issue(1, c.ctx) == "retries-exceeded"
        assert len(seen) == c.ctx.config.conflict_retries
        assert c.store.get_run(1).merge_conflicts == ["cli.py"]
        assert c.store.get_run(1).rebuild_count == 0
        assert "blocked" in c.tracker.get_issue(1).labels
        assert "cli.py" in c.tracker.list_comments(1)[-1].body
        return
    assert run_issue(1, c.ctx) == "merged"
    assert len(seen) == 1
    assert c.store.get_run(1).merge_conflicts == []
    assert "<<<<<<<" not in git_command(c.history.origin, "show", f"{branch}:cli.py")


def test_interrupted_conflict_is_rechecked_when_target_is_already_an_ancestor(conflicting_lanes, monkeypatch):
    c = conflicting_lanes
    set_done_when(c, [
        f"test -f {ACCEPTANCE}",
        "if test -f base-feature.txt && ! test -f fixed.txt; then printf '<<<<<<< HEAD\\n||||||| ancestor\\n=======\\n>>>>>>> target\\n' > cli.py; fi",
    ])
    resolved = "commands = ['help', 'base', 'issue']\n"
    c.executor.steps += [{"cli.py": resolved}, {}]
    execute = c.executor.run

    def interrupt(**kwargs):
        result = execute(**kwargs)
        return replace(result, timed_out=True) if len(c.executor.calls) == 3 else result

    monkeypatch.setattr(c.executor, "run", interrupt)
    assert run_issue(1, c.ctx) == "timeout"
    branch = "foragent/issue-1"
    assert c.ctx.git.is_ancestor(c.history.tip, git_command(c.history.clone, "rev-parse", branch))
    assert c.store.get_run(1).merge_conflicts == ["cli.py"]
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch=branch, worktree=c.store.worktree_path(1))
    c.executor.steps = [{}, {}, {"cli.py": resolved, "fixed.txt": "done"}]
    monkeypatch.setattr(c.executor, "run", execute)
    assert run_issue(1, c.ctx) == "merged"
    assert all("Resolve the conflict markers" in call["prompt"] for call in c.executor.calls[3:5])
    assert "|||||||" not in git_command(c.history.origin, "show", f"{branch}:cli.py")


def test_remote_conflicted_wip_is_rechecked_without_local_state(conflicting_lanes, monkeypatch, tmp_path):
    c = conflicting_lanes
    set_done_when(c, [
        f"test -f {ACCEPTANCE}",
        "if test -f base-feature.txt && ! test -f fixed.txt; then printf '<<<<<<< HEAD\\n||||||| ancestor\\n=======\\n>>>>>>> target\\n' > cli.py; fi",
    ])
    resolved = "commands = ['help', 'base', 'issue']\n"
    c.executor.steps += [{"cli.py": resolved}, {}]
    execute = c.executor.run

    def interrupt(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 1:
            c.move_target()
        return replace(result, timed_out=True) if len(c.executor.calls) == 3 else result

    monkeypatch.setattr(c.executor, "run", interrupt)
    assert run_issue(1, c.ctx) == "timeout"
    branch = "foragent/issue-1"
    assert git_command(c.history.origin, "show", f"{branch}:cli.py") == "<<<<<<< HEAD\n||||||| ancestor\n=======\n>>>>>>> target"
    assert c.ctx.git.is_ancestor(c.history.tip, git_command(c.history.clone, "rev-parse", branch))
    assert c.tracker.created_prs == []

    approval = c.store.load_approval(1)
    clone = tmp_path / "fresh-clone"
    git_command(tmp_path, "clone", str(c.history.origin), str(clone))
    c.history.clone = c.ctx.repo_root = clone
    c.ctx.git = GitRepo(clone, c.ctx.git._run)
    c.store = c.ctx.store = StateStore(tmp_path / "fresh-state", now=c.ctx.now)
    c.store.save_approval(**approval.__dict__)
    c.store.start_run(1, branch=branch, worktree=c.store.worktree_path(1))
    assert c.store.get_run(1).merge_conflicts == []
    c.executor.steps = [{}, {"cli.py": resolved, "fixed.txt": "done"}]
    monkeypatch.setattr(c.executor, "run", execute)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 5
    assert "충돌 마커가 남음: cli.py:1" in c.executor.calls[-1]["prompt"]
    assert git_command(c.history.origin, "show", f"{branch}:cli.py") == resolved.strip()


def test_conflict_resolution_has_full_budget_after_implementation_retries(conflicting_lanes):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, max_retries=1)
    c.executor.steps.insert(0, {"cli.py": "commands = ['help']\n"})
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 4 and c.store.get_run(1).attempts == 2
    assert "Conflict markers remain in: cli.py" in c.executor.calls[-1]["prompt"]
    assert c.store.load_flag(1) is None


@pytest.mark.parametrize("before_push", [False, True])
@pytest.mark.parametrize("scope", ["claude-local", "pr-body"])
def test_conflict_budget_rebuilds_once_on_latest_target_after_reviews(conflicting_lanes, monkeypatch, before_push, scope):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, max_retries=1, review_scope=scope)
    approval = c.store.load_approval(1)
    body = "## Goal\nPreserve the CLI feature.\n## Out of scope\nOther features.\n" + approval.body
    c.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)
    reviews = enable_reviewer(c, ["VERDICT: FAIL\ncli.py:1 wording", "VERDICT: PASS", "VERDICT: PASS"])
    c.executor.steps += [{}, {}, {}, {"cli.py": "commands = ['help', 'base', 'newbase', 'issue']\n"}]
    execute = c.executor.run
    latest = []
    leases = []

    def implement(**kwargs):
        index = len(c.executor.calls)
        root = kwargs["worktree"]
        if index == 4:
            leases.append(c.ctx.git.remote_branch_sha("foragent/issue-1") or "")
            assert (root / "cli.py").read_text() == "commands = ['help', 'base', 'newbase']\n"
            assert (root / "latest.txt").read_text() == "latest\n"
            assert (root / ".foragent/TRY.md").read_text() == "Try the feature\n"
            assert (root / ".foragent/REBUTTAL.md").read_text() == "- A finding: explanation\n"
            assert git_command(root, "rev-parse", "HEAD") == latest[0]
            assert not (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists()
            assert "earlier attempt could not be merged onto the moved base" in kwargs["prompt"]
            parsed = parse_body(c.store.load_approval(1).body)
            assert parsed.goal in kwargs["prompt"]
            assert parsed.out_of_scope in kwargs["prompt"]
            assert all(command in kwargs["prompt"] for command in parsed.done_when_commands)
            assert "Resolve the conflict markers" not in kwargs["prompt"]
        result = execute(**kwargs)
        if index == 0:
            (root / ".foragent").mkdir(exist_ok=True)
            (root / ".foragent/TRY.md").write_text("Try the feature\n")
            (root / ".foragent/REBUTTAL.md").write_text("- A finding: explanation\n")
            if before_push:
                c.move_target()
        if index == 3:
            c.history.update({"cli.py": "commands = ['help', 'base', 'newbase']\n", "latest.txt": "latest\n"})
            latest.append(c.history.tip)
        return result

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 5 and len(reviews) == 3
    assert c.store.get_run(1).attempts == 3
    assert c.store.load_flag(1) is None
    log = c.store.log_path(1).read_text()
    assert log.count("Rebuilding issue branch once") == 1
    assert "Conflict attempt 2/2" in log
    assert log.count("Done when command:") == 3
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == "commands = ['help', 'base', 'newbase', 'issue']"
    pushes = [args for _, args in c.operations if args[0] == "push"]
    first_rewrite = next(i for i, args in enumerate(pushes) if args[1].startswith("--force-with-lease="))
    assert all(args[1].startswith("--force-with-lease=") for args in pushes[first_rewrite:])
    assert pushes[first_rewrite][1] == f"--force-with-lease=refs/heads/foragent/issue-1:{leases[0]}"
    assert len(c.tracker.created_prs) == 1


@pytest.mark.parametrize("failure", ["checks", "review", "ci", "conflict"])
def test_failed_rebuild_blocks_without_more_implementation_or_resolution(conflicting_lanes, monkeypatch, failure):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, conflict_retries=1, max_retries=8)
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    if failure == "checks":
        c.executor.steps[-1] = {"cli.py": "commands = ['help', 'base']\n"}
    if failure == "review":
        enable_reviewer(c, ["VERDICT: PASS", "VERDICT: FAIL\ncli.py:1 still broken"])
    if failure in {"ci", "conflict"}:
        def ci(pr):
            c.ci_heads.append(c.tracker.pr_head_sha(pr))
            if len(c.ci_heads) == 1:
                c.move_target()
            elif failure == "conflict" and len(set(c.ci_heads)) > 1 and c.ci_heads.count(c.ci_heads[-1]) == 1:
                c.history.update({"cli.py": "commands = ['help', 'base', 'parallel']\n"})
            return "fail" if failure == "ci" and len(set(c.ci_heads)) > 1 else "pass"

        monkeypatch.setattr(c.tracker, "pr_checks", ci)
        c.tracker.set_pr_failed_logs(2, "The rebuilt CI failed")
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 3
    assert c.store.get_run(1).attempts == 2
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "blocked" in c.tracker.get_issue(1).labels
    assert c.tracker.merged_prs == []
    log = c.store.log_path(1).read_text()
    assert log.count("Rebuilding issue branch once") == 1
    assert "Rebuilt attempt failed:" in log
    if failure in {"checks", "review"}:
        assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.ci_heads[0]
    if failure == "ci":
        assert "The rebuilt CI failed" in c.tracker.list_comments(1)[-1].body


def test_conflict_budget_survives_ci_repair_and_republishing(conflicting_lanes, monkeypatch):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, conflict_retries=1)
    c.executor.steps += [
        {"cli.py": "commands = ['help', 'base', 'issue']\n"},
        {"repair.txt": "CI repair\n"},
        {"cli.py": "commands = ['help', 'base', 'parallel', 'issue']\n"},
    ]
    execute = c.executor.run

    def implement(**kwargs):
        index = len(c.executor.calls)
        result = execute(**kwargs)
        if index == 2:
            c.history.update({"cli.py": "commands = ['help', 'base', 'parallel']\n"})
        return result

    def ci(pr):
        c.ci_heads.append(c.tracker.pr_head_sha(pr))
        if len(c.ci_heads) == 1:
            c.move_target()
        return "fail" if len(set(c.ci_heads)) == 2 else "pass"

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    c.tracker.set_pr_failed_logs(2, "Repair the CI failure")
    # The used in-place budget survives CI repair; a minority conflict then stops instead of rebuilding (#416).
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 3
    assert "CI failed:\nRepair the CI failure" in c.executor.calls[2]["prompt"]
    log = c.store.log_path(1).read_text()
    assert log.count("Conflict attempt 1/1:") == 1
    assert log.count("Rebuilding issue branch once") == 0
    assert c.store.get_run(1).merge_conflicts == ["cli.py"]
    assert "blocked" in c.tracker.get_issue(1).labels


def test_fresh_base_done_when_failure_returns_to_implementer(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}, {"parallel.txt": "fixed"}]
    set_done_when(c, [COMMAND, 'test ! -f parallel.txt || test "$(cat parallel.txt)" = fixed'])
    count = []

    def ci(pr):
        count.append(pr)
        if len(count) == 1:
            c.history.update({"parallel.txt": "broken"})
        return "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert 'Command: test ! -f parallel.txt' in c.executor.calls[1]["prompt"]
    assert c.store.get_run(1).attempts == 2
    assert git_command(c.history.origin, "show", "foragent/issue-1:parallel.txt") == "fixed"


def test_moving_base_times_out_after_three_merges(run_case, monkeypatch):
    """The merge-turn rule (#712): no merge-count cap, only the run's time budget stops it."""
    c = run_case
    moves = []

    def ci(pr):
        moves.append(pr)
        c.now[0] += timedelta(minutes=20)
        c.history.update({f"parallel-{len(moves)}.txt": "fresh"})
        return "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == "timeout"
    # Every result-affecting move is merged and re-verified once, past the old cap of three.
    assert len(moves) == 5
    assert len([args for _, args in c.operations if "--no-edit" in args]) == len(moves)
    assert c.store.load_flag(1).reason == Reason.TIMEOUT
    assert not any("base kept moving" in comment.body for comment in c.tracker.list_comments(1))
    assert c.tracker.merged_prs == []


def test_replay_failure_without_conflicts_keeps_work_and_never_rebuilds(run_case, monkeypatch):
    """Zero conflicting own files never qualify for a from-scratch rebuild (#416)."""
    c = run_case
    c.history.update({"shared.txt": "base\n"})
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"sibling.txt": "sibling\n"})
    c.executor.steps[0].update({"shared.txt": "child\n"})

    def refuse(self, patch):
        raise GitError("error: corrupt patch at line 3")

    monkeypatch.setattr(GitRepo, "apply_staged_patch", refuse)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 1
    run = c.store.get_run(1)
    assert run.rebuild_count == 0 and run.rebuild_reason is None
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "corrupt patch" in c.tracker.list_comments(1)[-1].body
    # The PR's own work survives on its branch instead of the destination's clean tree.
    branch = "foragent/issue-1"
    assert git_command(c.history.origin, "show", f"{branch}:shared.txt") == "child"
    assert git_command(c.history.origin, "show", f"{branch}:feature.txt")


def test_replay_own_files_come_from_branch_head_not_uncommitted_staged_work(run_case):
    """Every staged file conflicts, but none is committed on the branch head: no rebuild (#416)."""
    c = run_case
    c.history.update({"shared.txt": "base\n"})
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"shared.txt": "sibling\n", "feature.txt": "sibling feature\n"})
    c.executor.steps[0].update({"shared.txt": "child\n"})
    c.executor.steps += [{}] * c.ctx.config.conflict_retries
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 1 + c.ctx.config.conflict_retries, "In-place resolution comes first"
    run = c.store.get_run(1)
    assert run.rebuild_count == 0 and run.rebuild_reason is None
    assert "shared.txt" in c.tracker.list_comments(1)[-1].body
    # The stop keeps the PR's own work rather than the destination tree holding replay markers.
    assert git_command(c.history.origin, "show", "foragent/issue-1:shared.txt") == "child"


def test_bug_steps_are_left_out_of_the_conflict_resolution_turn(conflicting_lanes, monkeypatch):
    c = conflicting_lanes
    c.tracker.add_labels(1, ["bug"])
    c.executor.steps.append({"cli.py": "commands = ['help', 'base', 'issue']\n"})
    implement = c.executor.run

    def run(**kwargs):
        initial = not c.executor.calls
        result = implement(**kwargs)
        if initial:
            c.move_target()
        return result

    monkeypatch.setattr(c.executor, "run", run)
    assert run_issue(1, c.ctx) == "merged"
    first, conflict = (call["prompt"] for call in c.executor.calls)
    assert "This issue is a bug. Follow these steps:" in first
    assert "Resolve the conflict markers" in conflict
    assert "This issue is a bug. Follow these steps:" not in conflict

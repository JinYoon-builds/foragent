import re
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import threading
from pathlib import Path
import shutil

import pytest

from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.reasons import Reason, parse_reason
from foragent.runner import _batch_pr, run_issue
from foragent.state import StateStore
from foragent.tracker import TrackerError

from conftest import ACCEPTANCE, BASE_TIME, git_command
from runner_support import BODY, COMMAND, batch_setup, enable_reviewer, run_case, seed_batch, set_done_when

# GitHub closes an issue named after one of these words when the PR merges.
CLOSING_KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#\d+", re.I)


def test_base_only_paths_merged_during_ci_do_not_retarget_the_pr(run_case, monkeypatch):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"sibling.txt": "batch"})
    seed_batch(c, {})
    moved = []

    def checks(pr):
        if not moved:
            c.history.update({"sibling.txt": "Merged by another issue during CI\n"})
            moved.append(c.history.tip)
        return "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    child = next(pr for pr in c.tracker.created_prs if pr.head == "foragent/issue-1")
    assert child.base == "main"
    assert c.tracker.merged_prs == [child.number]
    assert len(c.tracker.created_prs) == 1
    head = c.tracker.pr_head_sha(child.number)
    assert git_command(c.history.origin, "diff", "--name-only", moved[0], head) == "feature.txt"
    assert git_command(c.history.origin, "show", f"{head}:sibling.txt") == "Merged by another issue during CI"


def test_parallel_self_merges_cannot_install_an_older_tree_last(run_case, monkeypatch, tmp_path, capsys):
    from foragent import runner, tool_install

    c = run_case
    c.history.update({
        "pyproject.toml": '[project]\nname = "foragent"\n',
        "src/foragent/__init__.py": "VERSION = 0\n",
    })
    c.ctx.config = replace(c.ctx.config, test_command="true")
    approval = c.store.load_approval(1)
    c.tracker.add_issue(2, "Newer version", approval.body, labels=["approved", "running"])
    c.store.save_approval(**{**approval.__dict__, "number": 2})
    c.store.start_run(2, branch="foragent/issue-2", worktree=c.store.worktree_path(2))
    for pr in (3, 4):
        c.tracker.set_pr_files(pr, ["feature.txt", "src/foragent/__init__.py"])
        c.tracker.set_pr_checks(pr, "pass")
    packages = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter(()))
    first_captured, release_first = threading.Event(), threading.Event()
    local = threading.local()
    holder = [None]
    installed = []
    merge_lock, merge, after_merge = c.store.merge_lock, c.tracker.merge_pr, runner.after_merge

    def implement(**kwargs):
        local.number = int(kwargs["worktree"].name.removeprefix("issue-"))
        c.executor.calls.append(kwargs)
        (kwargs["worktree"] / "feature.txt").write_text("done")
        source = kwargs["worktree"] / "src/foragent/__init__.py"
        source.write_text(f"VERSION = {local.number}\n")
        return ExecResult(0, False, "done", kwargs["log_path"])

    @contextmanager
    def observed_lock():
        if local.number == 2 and holder[0] == 1:
            # B queues behind A's installation: let A finish to unblock B.
            release_first.set()
        with merge_lock():
            holder[0] = local.number
            try:
                yield
            finally:
                holder[0] = None

    def merge_into_main(pr, **kwargs):
        merge(pr, **kwargs)
        git_command(c.history.origin, "update-ref", "refs/heads/main", kwargs["expected_head_sha"])
        c.history.tip = kwargs["expected_head_sha"]

    def uv(args, *, cwd, env=None):
        if args[:2] == ["tool", "install"]:
            if local.number == 1:
                # _reinstall has already captured A's source at this point.
                first_captured.set()
                assert release_first.wait(10)
            package = Path(env["UV_TOOL_DIR"]) / "foragent/lib/python3.12/site-packages/foragent"
            shutil.copytree(Path(args[-1]) / "src/foragent", package)
            packages.append(package)
            for name in tool_install.ENTRY_POINTS:
                shim = Path(env["UV_TOOL_BIN_DIR"]) / name
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.symlink_to(package / "__init__.py")
            installed.append(local.number)
        else:
            assert args == ["tool", "dir", "--bin"]
        return str(tmp_path / "bin")

    def install_and_verify(*args):
        try:
            return after_merge(*args)
        finally:
            if local.number == 2:
                # With the lock released too early, B installs and verifies
                # before A resumes, reproducing a successful rollback to A.
                release_first.set()

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.store, "merge_lock", observed_lock)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_into_main)
    monkeypatch.setattr(tool_install, "run_uv", uv)
    monkeypatch.setattr(runner, "after_merge", install_and_verify)
    # This test pins the merge lock's scope; the FIFO turn (#712) would make B wait before the
    # lock and hide a lock released too early, so both runs contend for the lock directly.
    from foragent import run_merge_turn
    monkeypatch.setattr(run_merge_turn, "holds_turn", lambda store, place: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_issue, 1, c.ctx)
        try:
            assert first_captured.wait(10)
            second = pool.submit(run_issue, 2, c.ctx)
            assert second.result(timeout=15) == "merged"
            assert first.result(timeout=15) == "merged"
        finally:
            release_first.set()
    assert installed == [1, 2]
    package = packages[-1]
    assert (tmp_path / "bin/fa").resolve() == package / "__init__.py"
    assert (package / "__init__.py").read_text() == git_command(
        c.history.origin, "show", "main:src/foragent/__init__.py",
    ) + "\n"
    assert (package / "__init__.py").read_text() == "VERSION = 2\n"
    output = capsys.readouterr().out
    assert output.count("installed package matches merged source") == 2
    assert "reinstall failed" not in output


@pytest.mark.parametrize("publication", ["create_pr", "update_pr"])
@pytest.mark.parametrize("initial_move", [None, "docs/verified.md", "verified.py"])
def test_base_fetched_during_pr_publication_is_reverified(run_case, monkeypatch, publication, initial_move):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    set_done_when(c, [COMMAND, "test ! -f incompatible.txt"])
    if publication == "update_pr":
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="")
    if initial_move:
        execute = c.executor.run

        def implement(**kwargs):
            result = execute(**kwargs)
            c.history.update({initial_move: "Verified before publication\n"})
            return result

        monkeypatch.setattr(c.executor, "run", implement)
    publish = getattr(c.tracker, publication)
    moved = []

    def publish_then_fetch(*args, **kwargs):
        result = publish(*args, **kwargs)
        if not moved:
            c.history.update({"incompatible.txt": "The new base fails Done when\n"})
            moved.append(c.history.tip)
            c.ctx.git.fetch("origin", "main")
        return result

    monkeypatch.setattr(c.tracker, publication, publish_then_fetch)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(moved) == 1
    assert c.tracker.merged_prs == []
    log = c.store.log_path(1).read_text()
    assert f"base moved: result-affecting paths; re-verifying: {moved[0]}" in log
    assert "Command: test ! -f incompatible.txt" in log


@pytest.mark.parametrize("already_closed", [False, True])
def test_merge_explicitly_closes_issue_and_removes_ready_and_running(run_case, monkeypatch, already_closed):
    c = run_case
    c.tracker.add_labels(1, ["ready"])
    merge = c.tracker.merge_pr
    close = c.tracker.close_issue
    closed = []

    def merge_pr(pr, **kwargs):
        merge(pr, **kwargs)
        if already_closed:
            close(1)

    def close_issue(number):
        assert c.tracker.merged_prs == [2]
        assert c.tracker.get_issue(number).state == ("closed" if already_closed else "open")
        closed.append(number)
        if already_closed:
            raise TrackerError("Issue is already closed")
        close(number)

    monkeypatch.setattr(c.tracker, "merge_pr", merge_pr)
    monkeypatch.setattr(c.tracker, "close_issue", close_issue)
    assert run_issue(1, c.ctx) == "merged"
    assert closed == [1]
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.get_issue(1).labels == {"approved"}
    assert c.store.get_run(1).outcome == "merged"
    assert c.store.load_flag(1) is None


def test_merge_does_not_ignore_close_failure_for_open_issue(run_case, monkeypatch):
    c = run_case

    def close_issue(number):
        raise TrackerError("HTTP 403 Forbidden")

    monkeypatch.setattr(c.tracker, "close_issue", close_issue)
    assert run_issue(1, c.ctx) == "error"
    assert c.tracker.merged_prs == [2]
    assert c.tracker.get_issue(1).state == "open"
    assert "blocked" in c.tracker.get_issue(1).labels
    assert "HTTP 403 Forbidden" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("when", ["opened", "polling", "checks", "merge"])
def test_changed_pr_head_is_held_as_risk_high(run_case, monkeypatch, when):
    c = run_case

    def change_head():
        c.tracker.set_pr_head_sha(2, "f" * 40)
        c.tracker.set_pr_files(2, ["feature.txt", ACCEPTANCE])

    if when == "opened":
        create = c.tracker.create_pr

        def create_changed(**kwargs):
            pr = create(**kwargs)
            change_head()
            return pr

        monkeypatch.setattr(c.tracker, "create_pr", create_changed)
    elif when == "polling":
        c.tracker.set_pr_checks(2, "pending")

        def sleep(seconds):
            c.now[0] += timedelta(seconds=seconds)
            change_head()
            c.tracker.set_pr_checks(2, "pass")

        c.ctx.sleep = sleep
    elif when == "checks":
        monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: change_head() or "pass")
    else:
        merge = c.tracker.merge_pr

        def merge_changed(pr, **kwargs):
            change_head()
            merge(pr, **kwargs)

        monkeypatch.setattr(c.tracker, "merge_pr", merge_changed)

    assert run_issue(1, c.ctx) == "held"
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.RISK_HIGH
    assert "PR head changed after verification" in comment
    assert len([args for _, args in c.operations if args[:1] == ["push"]]) == 1


@pytest.mark.parametrize("extension", ["yml", "yaml"])
@pytest.mark.parametrize("existing_pr", [False, True])
def test_workflows_wait_three_minutes_before_trusting_no_checks(run_case, monkeypatch, extension, existing_pr):
    c = run_case
    c.executor.steps[0][f".github/workflows/ci.{extension}"] = "name: CI\n"
    if existing_pr:
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Closes #1")
    # Time spent implementing the issue must not consume the registration grace.
    c.now[0] += timedelta(minutes=2)
    opened_at = c.now[0]
    polls = []

    def checks(pr):
        polls.append(c.now[0])
        assert c.tracker.merged_prs == []
        return "none"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert polls == [opened_at + timedelta(seconds=30 * poll) for poll in range(7)] + [c.now[0]]
    assert c.now[0] == opened_at + timedelta(minutes=3)
    assert c.tracker.merged_prs == [2] and len(c.tracker.created_prs) == 1


@pytest.mark.parametrize("statuses,outcome,elapsed", [
    (["pass"], "merged", 0),
    (["fail"], "ci-failed", 0),
    (["none", "pending", "pass"], "merged", 60),
    (["none", "pending", "fail"], "ci-failed", 60),
    (["none", *(["pending"] * 7), "pass"], "merged", 240),
    (["pending"] * 41, "timeout", 1200),
])
def test_workflow_checks_keep_pass_failure_and_pending_behavior(run_case, monkeypatch, statuses, outcome, elapsed):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.executor.steps[0][".github/workflows/ci.yml"] = "name: CI\n"
    started_at = c.now[0]
    results = iter(statuses)
    monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: next(results, statuses[-1]))
    assert run_issue(1, c.ctx) == outcome
    assert c.now[0] == started_at + timedelta(seconds=elapsed)
    assert c.tracker.merged_prs == ([2] if outcome == "merged" else [])


@pytest.mark.parametrize("path", [".github/workflows/readme.txt", ".github/workflows/nested/ci.yml"])
def test_non_workflow_files_do_not_delay_no_checks_merge(run_case, path):
    c = run_case
    c.executor.steps[0][path] = "not a workflow\n"
    started_at = c.now[0]
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == started_at


def test_workflow_removed_from_pr_head_does_not_delay_merge(run_case, monkeypatch):
    c = run_case
    c.history.update({".github/workflows/ci.yml": "name: CI\n"})
    execute = c.executor.run

    def remove_workflow(**kwargs):
        result = execute(**kwargs)
        (kwargs["worktree"] / ".github/workflows/ci.yml").unlink()
        return result

    monkeypatch.setattr(c.executor, "run", remove_workflow)
    started_at = c.now[0]
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == started_at


def test_exhausted_retries_preserve_failed_branch_and_do_not_open_pr(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert c.tracker.created_prs == [] and c.tracker.merged_prs == []
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.RETRIES_EXCEEDED
    assert COMMAND in comment and "feature not ready" in comment
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "wrong"
    assert len([args for _, args in c.operations if args[:1] == ["push"]]) == 1


@pytest.mark.parametrize("reason", ["killed", "stalled", "stalled-restart", "error"])
def test_interrupted_worker_preserves_unstaged_work_with_recorded_reason(run_case, monkeypatch, reason):
    c = run_case
    c.executor.steps = [{"unfinished.txt": "partial implementation\n"}]
    execute = c.executor.run

    def interrupt(**kwargs):
        execute(**kwargs)
        if reason == "error":
            raise RuntimeError("Worker interrupted unexpectedly")
        # The scheduler records a stall before signalling the runner.
        if reason.startswith("stalled"):
            c.store.update_run(1, outcome=reason)
        raise SystemExit(143)

    monkeypatch.setattr(c.executor, "run", interrupt)
    if reason == "error":
        assert run_issue(1, c.ctx) == "error"
    else:
        with pytest.raises(SystemExit):
            run_issue(1, c.ctx)
    branch = "foragent/issue-1"
    for root in (c.history.clone, c.history.origin):
        assert git_command(root, "show", f"{branch}:unfinished.txt") == "partial implementation"
        assert f"[{reason}]" in git_command(root, "log", "-1", "--format=%B", branch)
        assert git_command(root, "rev-list", "--count", f"{c.history.tip}..{branch}") == "1"
    assert c.store.get_run(1).outcome == reason
    # A first stall leaves its labels to the next tick, which restarts it.
    labels = {"approved", "running"} if reason == "stalled-restart" else {"approved", "blocked"}
    assert c.tracker.get_issue(1).labels == labels
    local, = StateStore(c.store.root).local_issues()
    assert set(local["labels"]) == labels
    assert not c.tracker.created_prs and not c.tracker.merged_prs


def test_staged_runner_files_and_locked_edits_alone_do_not_create_wip(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{
        ACCEPTANCE: "def test_feature():\n    pass\n",
        ".foragent/QUESTION.md": "Choose A or B (recommended).\n",
        "CLAUDE.local.md": "Runner scope\n",
    }]
    execute = c.executor.run

    def stage(**kwargs):
        result = execute(**kwargs)
        git_command(kwargs["worktree"], "add", "-A")
        return result

    monkeypatch.setattr(c.executor, "run", stage)
    assert run_issue(1, c.ctx) == "spec-question"
    assert not any("commit" in args for _, args in c.operations)
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.history.tip


@pytest.mark.parametrize("reason", ["spec-question", "timeout", "retries-exceeded"])
@pytest.mark.parametrize("pr_timing", ["before-worker", "during-worker"])
def test_wip_stop_keeps_open_pr_head_unchanged(run_case, monkeypatch, reason, pr_timing):
    c = run_case
    branch = "foragent/issue-1"
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    git_command(c.history.origin, "update-ref", f"refs/heads/{branch}", c.history.tip)

    def open_pr():
        pr = c.tracker.create_pr(head=branch, base="main", title="Existing PR", body="Closes #1")
        c.tracker.set_pr_head_sha(pr, c.history.tip)

    if pr_timing == "before-worker":
        open_pr()
    c.executor.steps = [{"feature.txt": "unfinished"}]
    if reason == "spec-question":
        c.executor.steps[0][".foragent/QUESTION.md"] = "Choose A or B (recommended).\n"
    c.executor.timed_out = reason == "timeout"
    execute = c.executor.run

    def implement(**kwargs):
        result = execute(**kwargs)
        if pr_timing == "during-worker":
            open_pr()
        return result

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == reason
    assert git_command(c.history.clone, "show", f"{branch}:feature.txt") == "unfinished"
    assert f"[{reason}]" in git_command(c.history.clone, "log", "-1", "--format=%B", branch)
    assert git_command(c.history.origin, "rev-parse", branch) == c.history.tip
    assert c.tracker.pr_head_sha(c.tracker.find_pr(branch)) == c.history.tip
    assert not any(args[:1] == ["push"] for _, args in c.operations)
    assert len(c.tracker.created_prs) == 1 and not c.tracker.merged_prs


def test_wip_stop_pushes_when_previous_pr_is_closed(run_case):
    c = run_case
    branch = "foragent/issue-1"
    pr = c.tracker.create_pr(head=branch, base="main", title="Closed PR", body="Closes #1")
    c.tracker.close_issue(pr)
    c.executor.steps = [{"feature.txt": "unfinished", ".foragent/QUESTION.md": "Choose A or B.\n"}]

    assert run_issue(1, c.ctx) == "spec-question"
    assert git_command(c.history.origin, "show", f"{branch}:feature.txt") == "unfinished"
    assert git_command(c.history.origin, "rev-parse", branch) == git_command(c.history.clone, "rev-parse", branch)


def test_wip_stays_local_when_open_pr_lookup_fails(run_case, monkeypatch):
    c = run_case
    branch = "foragent/issue-1"
    c.executor.steps = [{"feature.txt": "unfinished", ".foragent/QUESTION.md": "Choose A or B.\n"}]

    def unavailable(head):
        raise TrackerError("GitHub unavailable during PR lookup")

    monkeypatch.setattr(c.tracker, "find_pr", unavailable)
    assert run_issue(1, c.ctx) == "spec-question"
    assert git_command(c.history.clone, "show", f"{branch}:feature.txt") == "unfinished"
    assert not any(args[:1] == ["push"] for _, args in c.operations)
    assert "open PR lookup failed" in c.store.log_path(1).read_text()


def test_tracker_failure_keeps_wip_local_without_further_pr_lookup(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "unfinished"}]
    execute = c.executor.run

    def interrupt(**kwargs):
        execute(**kwargs)
        raise TrackerError("GitHub returned HTTP 502")

    monkeypatch.setattr(c.executor, "run", interrupt)
    monkeypatch.setattr(c.tracker, "find_pr", lambda head: pytest.fail("Tracker already failed"))
    assert run_issue(1, c.ctx) == "error"
    assert git_command(c.history.clone, "show", "foragent/issue-1:feature.txt") == "unfinished"
    assert not any(args[:1] == ["push"] for _, args in c.operations)


@pytest.mark.parametrize("created", [False, True])
def test_missing_approved_path_does_not_discard_failed_implementation(run_case, created):
    c = run_case
    missing = "tests/acceptance/not_yet.py"
    approval = c.store.load_approval(1)
    c.store.save_approval(
        1, body=approval.body, body_sha=approval.body_sha,
        acceptance_blobs={**approval.acceptance_blobs, missing: "missing"},
        approved_by=approval.approved_by, approved_at=approval.approved_at,
    )
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.executor.steps = [{"feature.txt": "unfinished", **({missing: "tampered\n"} if created else {})}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    for root in (c.history.clone, c.history.origin):
        assert git_command(root, "show", "foragent/issue-1:feature.txt") == "unfinished"
        assert GitRepo(root).blob_sha("foragent/issue-1", missing) is None


@pytest.mark.parametrize("path,labels,config,reason", [
    (ACCEPTANCE, [], {}, Reason.ACCEPTANCE_TOUCHED),
    ("auth/nested/login.py", [], {"always_human": ["auth/**"]}, Reason.ALWAYS_HUMAN_PATH),
    ("feature.txt", ["risk:high"], {}, Reason.RISK_HIGH),
])
def test_pr_is_opened_and_held_by_policy(run_case, path, labels, config, reason):
    c = run_case
    c.executor.steps = [{"feature.txt": "done", path: "done"}]
    c.tracker.add_labels(1, labels)
    c.ctx.config = replace(c.ctx.config, **config)
    c.tracker.set_pr_files(2, ["feature.txt", path])
    assert run_issue(1, c.ctx) == "held"
    assert len(c.tracker.created_prs) == 1 and c.tracker.merged_prs == []
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == reason and "https://github.com/owner/repo/pull/2" in comment


@pytest.mark.parametrize("config", [
    {"review": "batch"}, {"review": "careful"}, {"review_paths": {"**/*.txt": "careful"}},
])
def test_non_fast_leaves_pr_open_without_needs_human(run_case, config):
    c = run_case
    c.ctx.config = replace(c.ctx.config, **config)
    assert run_issue(1, c.ctx) == "pr-open"
    assert c.tracker.get_issue(1).labels == {"approved"}
    assert c.store.load_flag(1) is None and c.tracker.merged_prs == []
    assert "PR #2 is waiting for your review" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("status,outcome,reason", [
    ("fail", "ci-failed", Reason.RETRIES_EXCEEDED), ("pending", "timeout", Reason.TIMEOUT),
])
def test_ci_failure_and_timeout(run_case, status, outcome, reason):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.tracker.set_pr_checks(2, status)
    assert run_issue(1, c.ctx) == outcome
    assert c.store.load_flag(1).reason == reason
    assert "https://github.com/owner/repo/pull/2" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.merged_prs == []
    if status == "pending":
        assert c.now[0] == BASE_TIME + timedelta(hours=1, minutes=20)


@pytest.mark.parametrize("ci_passes,repair_passes_done_when", [(True, True), (False, True), (True, False)])
def test_ci_retry_preserves_worktree_logs_and_all_checks(run_case, monkeypatch, ci_passes, repair_passes_done_when):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command="make check")
    reviews = enable_reviewer(c, ["VERDICT: PASS", "VERDICT: PASS"])
    logs = "first run\n" + "A" * 3990 + "\nsecond run\n" + "B" * 3990
    c.tracker.set_pr_checks(2, "fail")
    c.tracker.set_pr_failed_logs(2, logs)
    c.executor.steps.append({"feature.txt": "done" if repair_passes_done_when else "broken"})
    execute = c.executor.run
    worktree_identity = []

    def implement(**kwargs):
        path = kwargs["worktree"]
        worktree_identity.append(path.stat().st_ino)
        if c.executor.calls:
            assert (path / "feature.txt").read_text() == "done"
            assert (path / ".foragent/TRY.md").read_text() == "keep try"
            assert (path / ".foragent/REBUTTAL.md").read_text() == "keep rebuttal"
            c.tracker.set_pr_checks(2, "pass" if ci_passes else "fail")
        else:
            (path / ".foragent").mkdir()
            (path / ".foragent/TRY.md").write_text("keep try")
            (path / ".foragent/REBUTTAL.md").write_text("keep rebuttal")
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    expected = "merged" if ci_passes and repair_passes_done_when else "ci-failed" if not ci_passes else "retries-exceeded"
    assert run_issue(1, c.ctx) == expected
    assert len(c.executor.calls) == 2
    assert worktree_identity[0] == worktree_identity[1]
    assert "CI failed:\n" + logs in c.executor.calls[1]["prompt"]
    assert "`make check`" in c.executor.calls[1]["prompt"]
    assert "CI runs the full suite after publication" in c.executor.calls[1]["prompt"]
    assert "You run only the Done when tests and the tests for files you change" in c.executor.calls[1]["prompt"]
    assert len(reviews) == (2 if repair_passes_done_when else 1)
    assert c.store.get_run(1).attempts == 2
    assert len(c.tracker.created_prs) == 1
    if expected == "merged":
        assert not c.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    else:
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
        assert c.tracker.get_issue(1).labels & {"blocked", "needs-human"} == {"blocked"}
        if expected == "ci-failed":
            assert logs in c.tracker.list_comments(1)[-1].body


def test_ci_retry_after_base_refresh_keeps_merged_base_and_rereviews(run_case, monkeypatch):
    c = run_case
    reviews = enable_reviewer(c, ["VERDICT: PASS", "VERDICT: PASS"])
    c.executor.steps.append({"repair.txt": "fix CI on merged base"})
    c.tracker.set_pr_failed_logs(2, "failure after refreshing base")
    polls = []

    def checks(pr):
        polls.append(c.tracker.pr_head_sha(pr))
        if len(polls) == 1:
            c.history.update({"sibling.txt": "new base"})
        return "fail" if polls[-1] != polls[0] and len(set(polls)) == 2 else "pass"

    execute = c.executor.run

    def implement(**kwargs):
        if c.executor.calls:
            assert (kwargs["worktree"] / "sibling.txt").read_text() == "new base"
            assert c.ctx.git.at(kwargs["worktree"]).is_ancestor(c.history.tip, "HEAD")
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == len(reviews) == 2
    assert len(set(polls)) == 3
    assert "CI failed:\nfailure after refreshing base" in c.executor.calls[1]["prompt"]
    assert c.store.get_run(1).attempts == 2
    assert c.store.log_path(1).read_text().count("Done when command:") == 6


def test_ci_pending_polls_every_thirty_seconds_then_merges(run_case, monkeypatch):
    c = run_case
    statuses = iter(["pending", "pending", "pass"])
    monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: next(statuses, "pass"))
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == BASE_TIME + timedelta(hours=1, minutes=1)


def test_reuses_existing_pr(run_case):
    c = run_case
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Closes #1")
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1
    assert "## Review" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("retarget", [False, True])
def test_success_stores_try_card_verbatim_in_pr_but_not_git(run_case, provided, retarget):
    c = run_case
    if retarget:
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    card = "## What changed\n결과를 보여 줍니다.\n## Try it\nkind: cli\n```sh\ncat feature.txt\n```\n## Expect\ndone 표시\n"
    if provided:
        c.executor.steps[0][".foragent/TRY.md"] = card
    enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    body = c.tracker.created_prs[0].body
    saved = c.store.root / "try/1.md"
    if provided:
        assert saved.read_text() == card
        assert body.endswith("## Try it\n\n" + card)
    else:
        assert not saved.exists()
        assert body.endswith("## Try it\n\n_not provided by the implementer_")
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert ".foragent/" not in files


@pytest.mark.parametrize("outcome", ["merged", "held", "pr-open", "retries-exceeded", "retarget"])
def test_prestaged_try_and_other_artifacts_never_reach_branch(run_case, outcome):
    c = run_case
    if outcome == "retarget":
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    elif outcome == "held":
        c.tracker.add_labels(1, ["risk:high"])
    elif outcome == "pr-open":
        c.ctx.config = replace(c.ctx.config, review="careful")
    elif outcome == "retries-exceeded":
        c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "still wrong"}]
    card = "## Try it\nkind: cli\n```sh\ncat feature.txt\n```\n"
    implement = c.executor.run

    def prestage(**kwargs):
        result = implement(**kwargs)
        root = kwargs["worktree"]
        (root / ".foragent").mkdir(exist_ok=True)
        (root / ".foragent/TRY.md").write_text(card)
        (root / ".foragent/session.log").write_text("local execution artifact")
        git_command(root, "add", "-A")
        return result

    c.executor.run = prestage
    assert run_issue(1, c.ctx) == ("merged" if outcome == "retarget" else outcome)
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert ".foragent/" not in files and "feature.txt" in files
    if outcome in {"merged", "retarget"}:
        assert (c.store.root / "try/1.md").read_text() == card


@pytest.mark.parametrize("existing_batch", [False, True])
def test_batch_child_targets_shared_branch_closes_and_opens_parent_pr_once(run_case, existing_batch):
    c = run_case
    batch_setup(c)
    if existing_batch:
        seed_batch(c, {"sibling.txt": "previous child"})
    assert run_issue(1, c.ctx) == "merged"
    child, batch = c.tracker.created_prs
    assert child.base == "foragent/batch-99" and child.head == "foragent/issue-1"
    assert child.number in c.tracker.merged_prs and batch.number not in c.tracker.merged_prs
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.list_comments(1)[-1].body == "Merged into foragent/batch-99 (PR #100)"
    assert batch.head == "foragent/batch-99" and batch.base == "main"
    assert batch.title == "Batch: The whole feature (#99)"
    from foragent.i18n import text

    assert text("batch_pr_parent", c.ctx.config.language, parent=99) in batch.body and "#1: Implement feature (PR #100)" in batch.body
    assert not CLOSING_KEYWORD.search(batch.body), "Only the gate's order aggregation closes the parent"
    _batch_pr(99, c.ctx)
    assert len(c.tracker.created_prs) == 2
    if existing_batch:
        assert git_command(c.history.origin, "show", "foragent/issue-1:sibling.txt") == "previous child"
        assert not any(args[:2] == ["push", "origin"] for _, args in c.operations)
    else:
        assert any(args[:2] == ["push", "origin"] for _, args in c.operations)
    assert any(args == ["worktree", "add", "-B", "foragent/issue-1", str(c.store.worktree_path(1)), "origin/foragent/batch-99"]
               for _, args in c.operations)


def test_batch_waits_for_open_sibling_and_merge_unblocks_it(run_case):
    from foragent.scheduler import TickContext, tick
    from foragent.tracker import Dependency

    c = run_case
    batch_setup(c)
    c.tracker.add_issue(3, "Next child", BODY)
    c.tracker.record_label(3, "approved", "human")
    c.tracker.set_parent(3, 99)
    c.tracker.set_blocked_by(3, [Dependency(1, "open")])
    tick_ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=replace(c.ctx.config, approvers=["human"]), git=c.ctx.git, now=c.ctx.now,
        launcher=lambda number: None, start_runs=False,
    )
    tick(tick_ctx)
    assert "ready" not in c.tracker.get_issue(3).labels
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1
    tick(tick_ctx)
    assert "ready" in c.tracker.get_issue(3).labels


def test_reused_child_pr_is_retargeted_to_batch_and_gets_review_evidence(run_case):
    c = run_case
    batch_setup(c)
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Old body")
    enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    child = c.tracker.created_prs[0]
    assert child.base == "foragent/batch-99"
    assert "Round 1: VERDICT: PASS" in child.body


def test_tick_reconciles_manually_merged_careful_child_and_unblocks_sibling(run_case):
    from foragent.scheduler import TickContext, tick
    from foragent.tracker import Dependency

    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review_paths={"feature.txt": "careful"})
    c.tracker.add_issue(3, "Next child", BODY)
    c.tracker.record_label(3, "approved", "human")
    c.tracker.set_parent(3, 99)
    c.tracker.set_blocked_by(3, [Dependency(1, "open")])
    assert run_issue(1, c.ctx) == "pr-open"
    child = c.tracker.created_prs[0]
    assert child.base == "foragent/batch-99"
    c.tracker.merge_pr(child.number, expected_head_sha=c.tracker.pr_head_sha(child.number))
    assert c.tracker.get_issue(1).state == "open"
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=replace(c.ctx.config, approvers=["human"]), git=c.ctx.git, now=c.ctx.now,
        launcher=lambda number: pytest.fail("reconciliation must not launch"), start_runs=False,
    )
    tick(context)
    assert c.tracker.get_issue(1).state == "closed"
    assert not c.tracker.get_issue(1).labels & {"ready", "running"}
    assert c.tracker.list_comments(1)[-1].body == f"Merged into foragent/batch-99 (PR #{child.number})"
    assert "ready" in c.tracker.get_issue(3).labels
    assert len(c.tracker.created_prs) == 1
    # Closing the remaining child completes the parent on a later tick.
    c.tracker.close_issue(3)
    tick(context)
    assert len(c.tracker.created_prs) == 2
    assert c.tracker.created_prs[-1].head == "foragent/batch-99"
    comments = c.tracker.list_comments(1)
    tick(context)
    assert c.tracker.list_comments(1) == comments and len(c.tracker.created_prs) == 2


def test_tick_retries_batch_finalization_after_last_child_is_already_closed(run_case, monkeypatch):
    from foragent.scheduler import TickContext, tick

    c = run_case
    batch_setup(c)
    create = c.tracker.create_pr
    unavailable = [True]

    def create_pr(**kwargs):
        if kwargs["head"] == "foragent/batch-99" and unavailable[0]:
            raise TrackerError("HTTP 503")
        return create(**kwargs)

    monkeypatch.setattr(c.tracker, "create_pr", create_pr)
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.get_issue(1).state == "closed" and c.store.load_flag(1) is None
    assert len(c.tracker.created_prs) == 1
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker,
        store=StateStore(c.store.root, now=c.ctx.now), config=c.ctx.config,
        git=c.ctx.git, now=c.ctx.now, launcher=lambda number: None, start_runs=False,
    )
    assert any("HTTP 503" in action for action in tick(context).actions)
    unavailable[0] = False
    tick(context)
    assert len(c.tracker.created_prs) == 2
    batch = c.tracker.created_prs[-1]
    assert batch.head == "foragent/batch-99" and "PR #100" in batch.body
    tick(context)
    assert len(c.tracker.created_prs) == 2
    c.tracker.merge_pr(batch.number, expected_head_sha=c.tracker.pr_head_sha(batch.number))
    tick(context)
    assert len(c.tracker.created_prs) == 2


def test_generated_file_selects_batch_before_final_verification(run_case):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"generated.txt": "batch"})
    c.tracker.set_pr_files(100, ["feature.txt", "generated.txt"])
    seed_batch(c, {"sibling.txt": "previous child"})
    set_done_when(c, [COMMAND, "echo generated > generated.txt"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "foragent/batch-99"
    assert c.store.log_path(1).read_text().count("Done when command:") == 4


def test_retry_removing_batch_override_returns_only_child_patch_to_base(run_case, monkeypatch):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"batch.txt": "batch"})
    seed_batch(c, {"sibling.txt": "previous child"})
    c.executor.steps = [{"feature.txt": "done", "batch.txt": "unnecessary"}, {}]
    execute = c.executor.run

    def remove_batch_path(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 2:
            (kwargs["worktree"] / "batch.txt").unlink()
        return result

    monkeypatch.setattr(c.executor, "run", remove_batch_path)
    enable_reviewer(c, ["VERDICT: FAIL\nbatch.txt:1: remove this out-of-scope file", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "main"
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert "feature.txt" in files and "sibling.txt" not in files and "batch.txt" not in files


def test_batch_ci_failure_keeps_child_open_and_does_not_open_parent_pr(run_case):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.tracker.set_pr_checks(100, "fail")
    assert run_issue(1, c.ctx) == "ci-failed"
    assert c.tracker.get_issue(1).state == "open"
    assert not c.tracker.merged_prs and len(c.tracker.created_prs) == 1


def test_batch_siblings_build_on_actual_prior_merge(run_case, monkeypatch):
    c = run_case
    batch_setup(c)
    c.tracker.add_issue(2, "Second child", BODY, labels=["approved"])
    c.tracker.set_parent(2, 99)
    merge = c.tracker.merge_pr

    def merge_into_git_batch(pr, **kwargs):
        merge(pr, **kwargs)
        item = next(item for item in c.tracker.created_prs if item.number == pr)
        git_command(c.history.origin, "fetch", str(c.ctx.repo_root), f"+refs/heads/{item.head}:refs/heads/{item.base}")

    monkeypatch.setattr(c.tracker, "merge_pr", merge_into_git_batch)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1  # Sibling still open.
    approval = c.store.load_approval(1)
    second_body = BODY.replace("Implement the approved feature.", "Add the second feature.")
    second_body += "```sh\ntest -f second.txt\n```\n"
    c.store.save_approval(
        2, body=second_body, body_sha=body_hash(second_body), acceptance_blobs=approval.acceptance_blobs,
        approved_by="human", approved_at=c.now[0].isoformat(),
    )
    c.now[0] += timedelta(seconds=1)
    c.tracker.add_labels(2, ["running"])
    c.store.start_run(2, branch="foragent/issue-2", worktree=c.store.worktree_path(2))
    c.executor.steps = [{"second.txt": "done"}]
    assert run_issue(2, c.ctx) == "merged"
    assert git_command(c.history.origin, "show", "foragent/batch-99:feature.txt") == "done"
    assert git_command(c.history.origin, "show", "foragent/batch-99:second.txt") == "done"
    assert c.tracker.get_issue(1).state == c.tracker.get_issue(2).state == "closed"
    assert c.tracker.merged_prs == [100, 101]
    batch = c.tracker.created_prs[-1]
    assert batch.head == "foragent/batch-99" and "PR #100" in batch.body and "PR #101" in batch.body
    assert batch.number not in c.tracker.merged_prs


@pytest.mark.parametrize("scope", ["claude-local", "pr-body"])
@pytest.mark.parametrize("repair", [False, True])
def test_publication_returns_conflict_marker_locations_before_opening_pr(run_case, scope, repair):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review_scope=scope)
    reviews = enable_reviewer(c, ["VERDICT: PASS"])
    c.executor.steps = [{"feature.txt": "done", "notes.txt": "text\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> target\n"}]
    if repair:
        c.executor.steps.append({"notes.txt": "resolved\n"})
    else:
        c.executor.steps.append({})
    assert run_issue(1, c.ctx) == ("merged" if repair else "retries-exceeded")
    assert len(c.executor.calls) == 2
    for line in (2, 4, 6):
        assert f"충돌 마커가 남음: notes.txt:{line}" in c.executor.calls[-1]["prompt"]
    assert len(reviews) == int(repair)
    assert len(c.tracker.created_prs) == int(repair)
    assert len(c.tracker.merged_prs) == int(repair)


def test_publication_ignores_markers_in_unchanged_target_files(run_case):
    c = run_case
    c.history.update({"examples.txt": "<<<<<<< documented example\n=======\n>>>>>>> target\n"})
    c.executor.steps[0]["notes.rst"] = "Heading\n=======\n"
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 1


@pytest.mark.parametrize("content", [
    ">>>>>>> A valid nested blockquote\n", "||||||| ancestor\n", "Heading\n=======\n", "<<<<<<< example\n",
])
def test_publication_allows_standalone_marker_text(run_case, content):
    c = run_case
    c.executor.steps[0]["notes.md"] = content
    c.executor.steps.append({})
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 1 and len(c.tracker.created_prs) == 1


@pytest.mark.parametrize("existing", [False, True])
def test_batch_publication_checks_inherited_changes_against_actual_base(run_case, existing):
    from foragent.git import GitError

    c = run_case
    batch_setup(c)
    # Wait for a sibling so the ordinary child-relative publication can finish.
    c.tracker.add_issue(3, "Sibling")
    c.tracker.set_parent(3, 99)
    seed_batch(c, {"notes.txt": "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> target\n"})
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1
    c.tracker.close_issue(3)
    if existing:
        pr = c.tracker.create_pr(head="foragent/batch-99", base="main", title="Old title", body="Old body")
    before = list(c.tracker.created_prs)
    with pytest.raises(GitError, match="notes.txt:1"):
        _batch_pr(99, c.ctx)
    assert c.tracker.created_prs == before
    if existing:
        assert c.tracker.get_issue(pr).body == "Old body"


def test_batch_publication_failure_is_deferred_until_aggregate_contents_are_repaired(run_case):
    from foragent.scheduler import TickContext, tick

    c = run_case
    batch_setup(c)
    branch = "foragent/batch-99"
    seed_batch(c, {"notes.txt": "<<<<<<< HEAD\n=======\n>>>>>>> target\n"})
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.get_issue(1).state == "closed" and c.store.load_flag(1) is None
    assert len(c.tracker.created_prs) == 1
    assert "Batch PR finalization deferred to tick: 충돌 마커가 남음: notes.txt:1" in c.store.log_path(1).read_text()
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda number: None, start_runs=False,
    )
    assert any("notes.txt:1" in action for action in tick(context).actions)
    assert len(c.tracker.created_prs) == 1
    base = c.history.tip
    c.history.tip = c.ctx.git.remote_branch_sha(branch)
    c.history.update({"notes.txt": "resolved\n"})
    git_command(c.history.origin, "update-ref", f"refs/heads/{branch}", c.history.tip)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    c.history.tip = base
    tick(context)
    assert len(c.tracker.created_prs) == 2 and c.tracker.created_prs[-1].head == branch


def test_publication_rechecks_marker_locations_after_ci_before_merge(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "done", "notes.txt": "ready\n"}, {"notes.txt": "resolved\n"}]
    checks = c.tracker.pr_checks
    introduced = False

    def ci(pr):
        nonlocal introduced
        if not introduced:
            introduced = True
            root = c.store.worktree_path(1)
            (root / "notes.txt").write_text("<<<<<<< HEAD\n||||||| ancestor\n=======\n>>>>>>> target\n")
            git_command(root, "add", "--", "notes.txt")
        return checks(pr)

    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert "충돌 마커가 남음: notes.txt:1" in c.executor.calls[-1]["prompt"]
    assert len(c.tracker.merged_prs) == 1
    assert git_command(c.history.origin, "show", "foragent/issue-1:notes.txt") == "resolved"


def test_fresh_base_new_workflow_gets_ci_registration_grace(run_case, monkeypatch):
    c = run_case
    polls = []
    started = c.now[0]

    def checks(pr):
        polls.append(c.now[0])
        if len(polls) == 1:
            c.history.update({".github/workflows/ci.yml": "name: Newly added CI\n"})
        return "none"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert polls == [started, *[started + timedelta(seconds=30 * index) for index in range(7)], c.now[0]]
    assert c.now[0] == started + timedelta(minutes=3)


def test_parallel_lanes_implement_while_merge_phase_serializes_and_rechecks(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0, lanes={"ui": {}, "api": {}})
    set_done_when(c, ["test ! -f a || test ! -f b"])
    approval = c.store.load_approval(1)
    c.tracker.add_issue(2, "Other lane", approval.body, labels=["approved", "running"])
    c.store.save_approval(**{**approval.__dict__, "number": 2})
    c.store.update_run(1, lane="ui")
    c.store.start_run(2, branch="foragent/issue-2", worktree=c.store.worktree_path(2), lane="api")
    c.tracker.set_pr_files(3, ["a"])
    c.tracker.set_pr_files(4, ["b"])
    first_in_ci = threading.Event()
    second_in_ci = threading.Event()
    first_merged = threading.Event()
    release_first = threading.Event()
    local = threading.local()
    entries = []
    merge_lock = c.store.merge_lock
    merge = c.tracker.merge_pr

    def implement(**kwargs):
        number = 1 if kwargs["worktree"].name == "issue-1" else 2
        local.number = number
        c.executor.calls.append(kwargs)
        (kwargs["worktree"] / ("a" if number == 1 else "b")).write_text("implemented")
        return ExecResult(0, False, "done", kwargs["log_path"])

    @contextmanager
    def observed_lock():
        with merge_lock():
            entries.append(local.number)
            yield

    def checks(pr):
        if local.number == 1:
            if not first_in_ci.is_set():
                assert entries == []
                first_in_ci.set()
                assert release_first.wait(10)
            else:
                assert entries == [1]
        else:
            second_in_ci.set()
            assert first_merged.wait(10)
        return "pass"

    def merge_into_target(pr, **kwargs):
        assert entries == [1]
        merge(pr, **kwargs)
        git_command(c.history.origin, "update-ref", "refs/heads/main", kwargs["expected_head_sha"])
        first_merged.set()

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.store, "merge_lock", observed_lock)
    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_into_target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_issue, 1, c.ctx)
        try:
            assert first_in_ci.wait(10)
            second = pool.submit(run_issue, 2, c.ctx)
            assert second_in_ci.wait(10)
            assert len(c.executor.calls) == 2 and entries == []
            assert not second.done()
        finally:
            release_first.set()
        assert first.result(timeout=10) == "merged"
        assert second.result(timeout=10) == "retries-exceeded"
    assert entries == [1]  # The second lane refreshes its known stale base before queuing.
    assert len(c.tracker.merged_prs) == 1
    assert "Command: test ! -f a || test ! -f b" in c.tracker.list_comments(2)[-1].body
    assert git_command(c.history.origin, "ls-tree", "--name-only", "main") == "a\ntests"
    # Both lanes published before CI; the second failed fresh-base verification.
    second_log = c.store.log_path(2).read_text()
    assert second_log.count("Done when command:") == 2


@pytest.mark.parametrize("failure", [None, "HTTP 422", "API rate limit exceeded"])
def test_retitle_for_merge_falls_back_to_an_explicit_subject(failure):
    from types import SimpleNamespace

    from foragent.run_publish import retitle_for_merge

    edits, notes = [], []

    def update_pr(pr, **fields):
        edits.append((pr, fields))
        if failure:
            raise TrackerError(failure)

    ctx = SimpleNamespace(tracker=SimpleNamespace(update_pr=update_pr))

    def retitle(current):
        return retitle_for_merge(ctx, 2, title="Feature (#1)", current_title=current, base="main",
                                 body=lambda: "Closes #1", note=notes.append)

    assert retitle("Feature (#1)") == {} and not edits
    if failure and "rate limit" in failure:
        with pytest.raises(TrackerError):
            retitle("WIP: old (#1) [timeout]")
        assert not notes
        return
    expected = {"subject": "Feature (#1)"} if failure else {}
    assert retitle("WIP: old (#1) [timeout]") == expected
    assert edits == [(2, {"base": "main", "title": "Feature (#1)", "body": "Closes #1"})]
    assert bool(notes) == bool(failure)


def test_gate_merges_an_existing_wip_pr_with_an_explicit_subject_when_retitle_fails(run_case, monkeypatch):
    c = run_case
    wip = "WIP: Implement feature (#1) [timeout]"
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title=wip, body="Closes #1")
    c.tracker.set_pr_checks(pr, "pass")
    update, merge = c.tracker.update_pr, c.tracker.merge_pr
    edits, merges = [], []

    def update_pr(number, **fields):
        edits.append(fields["title"])
        if fields["title"] != wip:
            raise TrackerError("HTTP 422: title rejected")
        update(number, **fields)

    def merge_pr(number, **kwargs):
        merges.append(kwargs.get("subject"))
        merge(number, **{key: value for key, value in kwargs.items() if key != "subject"})

    monkeypatch.setattr(c.tracker, "update_pr", update_pr)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_pr)
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.merged_prs == [pr] and len(c.tracker.created_prs) == 1
    assert merges == ["Implement feature (#1)"]
    # The first refresh still retargets and updates the body under the old title.
    assert edits[:2] == ["Implement feature (#1)", wip] and edits[-1] == "Implement feature (#1)"
    assert "retitle failed" in c.store.log_path(1).read_text()


def test_gate_merges_with_an_explicit_subject_when_every_pr_edit_fails(run_case, monkeypatch):
    c = run_case
    wip = "WIP: Implement feature (#1) [timeout]"
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title=wip, body="Closes #1")
    c.tracker.set_pr_checks(pr, "pass")
    merge = c.tracker.merge_pr
    edits, merges = [], []

    def update_pr(number, **fields):
        edits.append(fields["title"])
        raise TrackerError("HTTP 500: server error")

    def merge_pr(number, **kwargs):
        merges.append(kwargs.get("subject"))
        merge(number, **{key: value for key, value in kwargs.items() if key != "subject"})

    monkeypatch.setattr(c.tracker, "update_pr", update_pr)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_pr)
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.merged_prs == [pr] and len(c.tracker.created_prs) == 1
    assert merges == ["Implement feature (#1)"]
    assert edits[:2] == ["Implement feature (#1)", wip] and edits[-1] == "Implement feature (#1)"
    log = c.store.log_path(1).read_text()
    assert "retitle failed" in log and "HTTP 500" in log


def _merge_batch_then_tick(c):
    """Merge the batch PR right after runner finalization, before any gate tick."""
    from foragent.scheduler import TickContext, tick

    assert run_issue(1, c.ctx) == "merged"
    batch = c.tracker.created_prs[-1]
    assert batch.head == "foragent/batch-99"
    assert not CLOSING_KEYWORD.search(batch.body), "A batch PR merge must not close the order parent"
    c.tracker.merge_pr(batch.number, expected_head_sha=c.tracker.pr_head_sha(batch.number))
    assert c.tracker.get_issue(99).state == "open"
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now,
        launcher=lambda number: pytest.fail("parent completion must not launch"), start_runs=False,
    )
    tick(context)
    return context


def test_batch_pr_merged_before_any_tick_leaves_parent_completion_to_the_gate(run_case):
    from foragent.i18n import text
    from foragent.scheduler import tick

    c = run_case
    batch_setup(c)
    before = c.tracker.list_comments(99)
    context = _merge_batch_then_tick(c)
    parent = c.tracker.get_issue(99)
    assert parent.state == "closed", "The gate closes the order once every child is done"
    completion = text("order_record_closed", c.ctx.config.language, count=1)
    added = c.tracker.list_comments(99)[len(before):]
    assert [comment.body for comment in added].count(completion) == 1
    tick(context)
    assert [comment.body for comment in c.tracker.list_comments(99)[len(before):]] == [
        comment.body for comment in added
    ]


def test_batch_pr_merge_keeps_a_parent_with_a_not_planned_child_open_and_flagged(run_case):
    from dataclasses import replace as replace_issue

    from foragent.board_parents import NOT_PLANNED_FLAG

    c = run_case
    batch_setup(c)
    dropped = c.tracker.add_issue(3, "Dropped part", state="closed")
    c.tracker.issues[3] = replace_issue(dropped, state_reason="not_planned")
    c.tracker.set_parent(3, 99)
    _merge_batch_then_tick(c)
    assert c.tracker.get_issue(99).state == "open", "A not-planned child keeps the order open"
    flag = c.store.load_flag(99)
    assert flag is not None and flag.reason == NOT_PLANNED_FLAG

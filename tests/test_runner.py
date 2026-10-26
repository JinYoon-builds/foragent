from dataclasses import replace
from datetime import timedelta
import json
import os
import subprocess
import signal
import sys
import time

import pytest

from foragent.reasons import Reason
from foragent.runner import run_issue
from foragent.scheduler import TickContext, TickReport, _kill_run, _recover_orphan, _recover_runs

from conftest import ACCEPTANCE, BASE_TIME, git_command
# Keep the shared setup importable by the existing scheduler, orch and session tests.
from runner_support import BODY, enable_reviewer, import_index, run_case


@pytest.mark.parametrize("ci", ["pass", "none"])
def test_happy_path_merges_and_records_results(run_case, monkeypatch, ci):
    c = run_case
    c.tracker.set_pr_checks(2, ci)
    merge = c.tracker.merge_pr
    merged_heads = []

    def merge_verified(pr, *, method, expected_head_sha):
        assert expected_head_sha == git_command(c.history.origin, "rev-parse", "foragent/issue-1")
        merged_heads.append(expected_head_sha)
        merge(pr, method=method, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(c.tracker, "merge_pr", merge_verified)
    assert run_issue(1, c.ctx) == "merged"
    assert len(merged_heads) == 1
    assert f"Pushed verified HEAD: {merged_heads[0]}" in c.store.log_path(1).read_text()
    assert c.tracker.merged_prs == [2]
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.get_issue(1).labels == {"approved"}
    pr = c.tracker.created_prs[0]
    assert pr.head == "foragent/issue-1" and pr.base == "main"
    assert pr.title == "Implement feature (#1)" and "Closes #1" in pr.body
    assert "| pass |" in pr.body and "## Needs a human check" in pr.body
    assert "- [ ] Looks right to a human" in pr.body and "- [x]" not in pr.body
    assert "Opened by Foragent gate (attempt 1)" in pr.body
    assert c.store.get_run(1).pid == os.getpid()
    assert c.store.get_run(1).outcome == "merged"
    assert "Check exit_code=0" in c.store.log_path(1).read_text()
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert c.now[0] == BASE_TIME + timedelta(hours=1)


@pytest.mark.parametrize("mode,question,outcome,statuses,column", [
    ("fast", False, "merged", ["Running", "PR open", "Done"], "📦 Done"),
    ("careful", False, "pr-open", ["Running", "PR open"], "🐿 In progress"),
    ("fast", True, "spec-question", ["Running", "Blocked"], "🐿 In progress"),
])
def test_runner_updates_project_on_execution_pr_merge_and_block(run_case, monkeypatch, mode, question, outcome, statuses, column):
    from foragent import board
    from test_board import item_values

    c = run_case
    project_id = board.ensure(c.tracker, c.ctx.repo)
    c.ctx.config = replace(c.ctx.config, board=project_id, review=mode)
    if question:
        c.executor.steps = [{".foragent/QUESTION.md": "Which behavior is intended?"}]
    calls = []
    set_status = board.set_status

    def recorded(issue, status):
        calls.append(status)
        set_status(issue, status)

    monkeypatch.setattr(board, "set_status", recorded)
    assert run_issue(1, c.ctx) == outcome
    assert calls == statuses
    assert item_values(c.tracker, project_id, 1)["Status"] == column


def test_board_transport_failure_does_not_fail_runner(run_case, caplog):
    from foragent import board

    c = run_case
    c.ctx.config = replace(c.ctx.config, board=board.ensure(c.tracker, c.ctx.repo))

    def unavailable(*args):
        raise OSError("offline project")

    c.tracker.graphql = unavailable
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.get_issue(1).state == "closed"
    assert "Could not set board Status" in caplog.text


@pytest.mark.parametrize("saved_at", ["local", "origin"])
def test_retry_reads_saved_branch_before_worker_even_without_both_copies(run_case, monkeypatch, saved_at):
    c = run_case
    branch = "foragent/issue-1"
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    c.executor.steps = [{"feature.txt": "unfinished"}]
    original_git_run = c.ctx.git._run

    def unavailable_push(args):
        command = args
        while command[:1] == ["-C"]:
            command = command[2:]
        if command[:1] == ["push"]:
            raise subprocess.CalledProcessError(1, ["git", *args], stderr="Origin unavailable")
        return original_git_run(args)

    with monkeypatch.context() as patch:
        if saved_at == "local":
            patch.setattr(c.ctx.git, "_run", unavailable_push)
        assert run_issue(1, c.ctx) == ("wip-not-saved" if saved_at == "local" else "retries-exceeded")
    # A failed push is a failed save: the runner keeps its worktree and blocks the issue.
    assert c.store.worktree_path(1).exists() == (saved_at == "local")
    assert ("blocked" in c.tracker.get_issue(1).labels) and "running" not in c.tracker.get_issue(1).labels
    if saved_at == "origin":
        # Model a new clone that has the remote WIP but no local issue branch.
        git_command(c.history.clone, "update-ref", "-d", f"refs/heads/{branch}")
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch=branch, worktree=c.store.worktree_path(1))
    c.executor.steps = [{"feature.txt": "done"}]
    execute = c.executor.run

    def finish(**kwargs):
        assert (kwargs["worktree"] / "feature.txt").read_text() == "unfinished"
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", finish)
    assert run_issue(1, c.ctx) == "merged"
    assert git_command(c.history.origin, "show", f"{branch}:feature.txt") == "done"


@pytest.mark.parametrize("cleanup", ["kill", "crash", "orphan"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_scheduler_preserves_wip_after_real_process_death(run_case, fake_process_table, cleanup, signum):
    c, table = run_case, fake_process_table
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "unfinished.txt").write_text("partial implementation\n")
    (path / ACCEPTANCE).write_text("tampered\n")
    (path / ".foragent").mkdir()
    (path / ".foragent/session.log").write_text("runner artifact")
    root = table.add(42000, cwd=path, start_time=10)
    c.store.update_run(1, pid=root.pid)

    def terminate(pid):
        if pid in table.live:
            table.kill(pid, signum)

    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
        kill_runner=terminate,
    )
    report = TickReport()
    if cleanup == "kill":
        _kill_run(ctx, 1, report)
    else:
        terminate(root.pid)
        if cleanup == "crash":
            _recover_runs(ctx, report)
        else:
            c.store.finish_run(1, "error")
            c.now[0] += timedelta(seconds=1)
            _recover_orphan(ctx, c.tracker.get_issue(1), report)
    assert root.pid not in table.live
    assert not path.exists()
    for root in (c.history.clone, c.history.origin):
        assert git_command(root, "show", "foragent/issue-1:unfinished.txt") == "partial implementation"
        assert git_command(root, "rev-parse", f"foragent/issue-1:{ACCEPTANCE}") == (
            c.store.load_approval(1).acceptance_blobs[ACCEPTANCE]
        )
        assert ".foragent" not in git_command(root, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    c.tracker.remove_labels(1, ["running"])


@pytest.mark.parametrize("cleanup", ["kill", "crash", "orphan"])
@pytest.mark.parametrize("failure", ["stage", "push"])
def test_scheduler_preservation_failure_keeps_worktree_and_blocks_issue(run_case, monkeypatch, cleanup, failure):
    c = run_case
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "unfinished.txt").write_text("partial implementation\n")
    git_run = c.ctx.git._run

    def unavailable(args):
        command = args
        while command[:1] == ["-C"]:
            command = command[2:]
        if (failure == "stage" and command[:2] == ["ls-files", "--modified"]) or (
            failure == "push" and command[:1] == ["push"]
        ):
            raise subprocess.CalledProcessError(1, ["git", *args], stderr=f"{failure} unavailable")
        return git_run(args)

    monkeypatch.setattr(c.ctx.git, "_run", unavailable)
    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
        runner_process_exists=lambda n: False,
    )
    report = TickReport()
    if cleanup == "kill":
        _kill_run(ctx, 1, report)
    elif cleanup == "crash":
        c.now[0] += timedelta(minutes=3)
        _recover_runs(ctx, report)
    else:
        c.store.finish_run(1, "error")
        c.now[0] += timedelta(seconds=1)
        _recover_orphan(ctx, c.tracker.get_issue(1), report)
    # A failed save never removes the only copy, nor restarts the issue as a crash.
    assert (path / "unfinished.txt").read_text() == "partial implementation\n"
    run = c.store.get_run(1)
    assert run.status == "finished"
    assert run.outcome == ("error" if cleanup == "orphan" else "wip-not-saved")
    assert c.tracker.get_issue(1).labels & {"blocked", "ready", "running"} == {"blocked"}
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    events = [json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    failures = [event for event in events if event["kind"] == "wip-preservation-failed"]
    assert len(failures) == 1 and f"{failure} unavailable" in failures[0]["summary"]
    assert "worktree kept" in failures[0]["summary"]
    assert not any(event["kind"] == "crash-restart" for event in events)
    assert "WIP preservation failed" in c.store.log_path(1).read_text()
    assert any("WIP preservation failed" in action for action in report.actions)
    if failure == "push":
        assert git_command(c.history.clone, "show", "foragent/issue-1:unfinished.txt") == "partial implementation"
    monkeypatch.setattr(c.ctx.git, "_run", git_run)
    c.ctx.git.remove_worktree(path)  # A person recovers the kept work.


def test_scheduler_keeps_unverified_wip_local_with_open_pr(run_case):
    c = run_case
    branch = "foragent/issue-1"
    git_command(c.history.origin, "update-ref", f"refs/heads/{branch}", c.history.tip)
    c.tracker.create_pr(head=branch, base="main", title="Existing PR", body="Closes #1")
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, branch, "origin/main")
    (path / "unfinished.txt").write_text("partial implementation\n")
    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
    )
    _kill_run(ctx, 1, TickReport())
    assert git_command(c.history.clone, "show", f"{branch}:unfinished.txt") == "partial implementation"
    assert git_command(c.history.origin, "rev-parse", branch) == c.history.tip
    assert not any(args[:1] == ["push"] for _, args in c.operations)
    c.tracker.remove_labels(1, ["running"])


@pytest.mark.parametrize("content", [None, "changed acceptance\n"])
def test_fetched_worktree_blob_mismatch_invalidates_approval(run_case, content):
    c = run_case
    c.history.update({ACCEPTANCE: content})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_approval(1) is None and c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert c.tracker.get_issue(1).labels == {"blocked"}
    assert c.executor.calls == [] and c.tracker.created_prs == []


@pytest.mark.parametrize("result", [
    "merged", "pr-open", "no-approval", "retries-exceeded", "limit-wait",
    RuntimeError("unexpected execution failure"), SystemExit(1),
])
def test_runner_kicks_after_cleanup_and_finish_for_every_outcome(run_case, monkeypatch, result, kicks):
    c = run_case

    def execute(number, ctx, worktree):
        ctx.tracker.remove_labels(number, ["running"])
        if isinstance(result, BaseException):
            raise result
        return result

    def kick(root):
        assert root == c.ctx.repo_root
        assert c.store.get_run(1).status == "finished"
        assert not c.store.worktree_path(1).exists()
        assert len(c.finished) == 1
        kicks.append(root)

    monkeypatch.setattr("foragent.runner._execute", execute)
    monkeypatch.setattr("foragent.runner.kick_tick", kick)
    if isinstance(result, SystemExit):
        with pytest.raises(SystemExit):
            run_issue(1, c.ctx)
    else:
        assert run_issue(1, c.ctx) == ("error" if isinstance(result, Exception) else result)
    assert kicks == [c.ctx.repo_root]
    assert c.store.get_run(1).outcome == (
        "killed" if isinstance(result, SystemExit) else "error" if isinstance(result, Exception) else result
    )


@pytest.mark.parametrize(("recorded", "labels"), [
    ("stalled-restart", {"running"}),
    ("stalled", {"blocked"}),
    (None, {"blocked"}),
])
def test_runner_stop_leaves_first_stall_labels_to_the_tick(run_case, monkeypatch, recorded, labels):
    c = run_case

    def execute(number, ctx, worktree):
        if recorded is not None:
            ctx.store.update_run(number, outcome=recorded)
        raise SystemExit(143)

    monkeypatch.setattr("foragent.runner._execute", execute)
    monkeypatch.setattr("foragent.runner.kick_tick", lambda root: None)
    with pytest.raises(SystemExit):
        run_issue(1, c.ctx)
    assert c.tracker.get_issue(1).labels & {"running", "ready", "blocked"} == labels
    assert c.store.get_run(1).outcome == (recorded or "killed")
    c.tracker.remove_labels(1, ["running"])  # the next tick restores it to ready


def test_runner_stop_leaves_orchestrator_stop_labels_and_reason_to_the_verb(run_case, monkeypatch):
    c = run_case

    def execute(number, ctx, worktree):
        # `fa orch stop` records its outcome and why before signaling the runner.
        ctx.store.update_run(number, outcome="stopped", detail="stopped: reprioritize")
        raise SystemExit(143)

    monkeypatch.setattr("foragent.runner._execute", execute)
    monkeypatch.setattr("foragent.runner.kick_tick", lambda root: None)
    with pytest.raises(SystemExit):
        run_issue(1, c.ctx)
    assert c.tracker.get_issue(1).labels & {"running", "ready", "blocked"} == {"running"}
    run = c.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", "stopped: reprioritize")
    c.tracker.remove_labels(1, ["running"])  # `fa orch stop` then returns it to ready


def test_runner_still_kicks_when_a_tick_already_finished_its_record(run_case, monkeypatch, kicks):
    c = run_case

    def execute(number, ctx, worktree):
        ctx.tracker.remove_labels(number, ["running"])
        ctx.store.finish_run(number, "killed")
        return "no-approval"

    monkeypatch.setattr("foragent.runner._execute", execute)
    assert run_issue(1, c.ctx) == "no-approval"
    assert c.store.get_run(1).outcome == "killed"
    assert len(c.finished) == 1
    assert kicks == [c.ctx.repo_root]


def test_missing_approval_finishes_without_execution(run_case):
    c = run_case
    c.store.clear_approval(1)
    assert run_issue(1, c.ctx) == "no-approval"
    assert c.executor.calls == [] and c.store.load_flag(1) is None


def test_stale_worktree_removed_and_rebuilt(run_case):
    c = run_case
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "stale.txt").write_text("stale")
    assert run_issue(1, c.ctx) == "merged"
    assert "stale.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


def test_unexpected_exception_is_logged_flagged_and_finished_once(run_case):
    c = run_case
    c.executor.steps = [RuntimeError("unexpected\nexecutor error")]
    assert run_issue(1, c.ctx) == "error"
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Runner failed: unexpected executor error" in c.tracker.list_comments(1)[-1].body
    assert "Traceback" in c.store.log_path(1).read_text()
    assert "RuntimeError: unexpected" in c.store.log_path(1).read_text()


def test_missing_machine_check_cannot_open_or_merge_pr(run_case):
    c = run_case
    approval = c.store.load_approval(1)
    c.store.save_approval(**{
        **approval.__dict__, "body": "## Goal\nMissing commands\n## Done when\n- [ ] Looks good\n",
    })
    assert run_issue(1, c.ctx) == "no-machine-check"
    assert c.executor.calls == [] and c.tracker.created_prs == []
    assert c.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK


def test_tracker_exception_after_push_still_cleans_up_and_finishes(run_case, monkeypatch):
    c = run_case

    def unavailable(**kwargs):
        raise RuntimeError("PR creation unavailable")

    monkeypatch.setattr(c.tracker, "create_pr", unavailable)
    assert run_issue(1, c.ctx) == "error"
    assert "PR creation unavailable" in c.store.log_path(1).read_text()
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED

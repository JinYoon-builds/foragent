"""Pre-merge isolation, full-suite discovery, and freshness at the merge boundary."""

from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME, git_command
from foragent import orch
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError


@pytest.fixture
def merge_case(git_history, tmp_path):
    h = git_history
    h.update({"feature.txt": "PR version", "suite-ok": "yes",
              "pyproject.toml": "[project]\nname = 'sample'\n",
              "package.json": '{"scripts":{"test":"run-suite"}}',
              "pnpm-lock.yaml": "", "go.mod": "module sample\n"})
    head = h.tip
    git_command(h.origin, "update-ref", "refs/heads/foragent/batch-1", head)
    h.update({"base.txt": "base advanced"})
    git = GitRepo(h.clone)
    tracker = FakeTracker(now=lambda: BASE_TIME)
    pr = tracker.create_pr(head="foragent/batch-1", base="main", title="Batch", body="Keep this body")
    tracker.set_pr_head_sha(pr, head)
    tracker.set_pr_checks(pr, "pass")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    config = Config(test_command="test -f suite-ok", repo="owner/repo")
    ctx = RunContext("owner/repo", h.clone, tracker, store, config, git, now=lambda: BASE_TIME)
    (h.clone / "local-draft.txt").write_text("keep me")
    before = git_command(h.clone, "status", "--porcelain")
    worktrees = git_command(h.clone, "worktree", "list", "--porcelain")
    yield SimpleNamespace(ctx=ctx, tracker=tracker, pr=pr, head=head, history=h)
    assert git_command(h.clone, "status", "--porcelain") == before
    assert git_command(h.clone, "worktree", "list", "--porcelain") == worktrees
    assert (h.clone / "local-draft.txt").read_text() == "keep me"


def test_detects_and_runs_every_full_suite_from_pr_head(merge_case, tmp_path, monkeypatch):
    c = merge_case
    c.ctx.config = replace(c.ctx.config, test_command=None)
    programs = tmp_path / "programs"
    programs.mkdir()
    record = tmp_path / "commands.txt"
    for name in ("uv", "pnpm", "go"):
        program = programs / name
        program.write_text('#!/bin/sh\nprintf "%s:%s\\n" "${0##*/}" "$*" >> "$PREMERGE_RECORD"\ntest -f suite-ok\n')
        program.chmod(0o755)
    monkeypatch.setenv("PATH", str(programs) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PREMERGE_RECORD", str(record))
    assert orch.premerge(c.ctx, c.pr)["ready"] is True
    assert record.read_text().splitlines() == ["uv:run pytest", "pnpm:test", "go:test ./..."]
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_test_artifacts_do_not_affect_trial_merge(merge_case):
    c = merge_case
    c.ctx.config = replace(c.ctx.config, test_command="printf dirty > feature.txt; printf generated > base.txt")
    report = orch.premerge(c.ctx, c.pr)
    assert report["conflicts"] == [] and report["ready"] is True
    assert c.tracker.pr_head_sha(c.pr) == c.head


@pytest.mark.parametrize("change", ["head", "base", "retarget", "closed", "ci-fail", "ci-pending"])
def test_changes_during_premerge_refuse_merge_before_attribution(merge_case, monkeypatch, change):
    c = merge_case
    calls = []

    def checks(pr):
        calls.append(pr)
        if len(calls) == 1:
            if change == "head":
                c.tracker.set_pr_head_sha(pr, "f" * 40)
            elif change == "base":
                c.history.update({"another.txt": "new base"})
            elif change == "retarget":
                git_command(c.history.origin, "update-ref", "refs/heads/other", c.history.tip)
                c.tracker.update_pr(pr, base="other", title="Batch", body="Keep this body")
            elif change == "closed":
                c.tracker.close_pr(pr)
            return "pass"
        return "pending" if change == "ci-pending" else "fail"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    with pytest.raises(orch.OrchError):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert not c.tracker.merged_prs
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_match_head_guard_catches_change_after_approval_comment(merge_case, monkeypatch):
    c = merge_case
    comment = c.tracker.comment

    def changed(pr, body):
        result = comment(pr, body)
        c.tracker.set_pr_head_sha(pr, "f" * 40)
        return result

    monkeypatch.setattr(c.tracker, "comment", changed)
    with pytest.raises(TrackerError, match="head changed"):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert not c.tracker.merged_prs
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


@pytest.mark.parametrize("failure", ["timeout", "exception", "interrupted", "merge-error", "no-command"])
def test_failure_cleanup_never_leaves_temporary_worktrees(merge_case, monkeypatch, failure):
    c = merge_case
    if failure == "timeout":
        monkeypatch.setattr(orch, "run_logged", lambda *a, **kw: (None, True, ""))
        assert orch.premerge(c.ctx, c.pr)["tests"] == "fail"
        return
    if failure == "no-command":
        c.ctx.config = replace(c.ctx.config, test_command=None)
        monkeypatch.setattr(orch, "detect_test_commands", lambda root: [])
        error = orch.OrchError
    elif failure == "merge-error":
        call = c.ctx.git._call

        def failed(args):
            if "merge" in args:
                raise GitError("merge failed without conflicts")
            return call(args)

        monkeypatch.setattr(c.ctx.git, "_call", failed)
        error = GitError
    else:
        error = OSError if failure == "exception" else SystemExit

        def failed(*a, **kw):
            raise error("test command interrupted")

        monkeypatch.setattr(orch, "run_logged", failed)
    with pytest.raises(error):
        orch.premerge(c.ctx, c.pr)
    assert not c.tracker.merged_prs
    assert not c.tracker.comments


@pytest.mark.parametrize("action", ["premerge", "merge"])
@pytest.mark.parametrize("failed_attempt", [1, 2])
def test_worktree_creation_failure_preserves_original_error(merge_case, monkeypatch, action, failed_attempt):
    c = merge_case
    add_worktree = c.ctx.git.add_detached_worktree
    creation_error = GitError("PR head object is unavailable")
    attempts = 0

    def add(path, ref):
        nonlocal attempts
        attempts += 1
        if attempts == failed_attempt:
            # A failed creation can leave a directory without a registered worktree.
            path.mkdir()
            raise creation_error
        add_worktree(path, ref)

    monkeypatch.setattr(c.ctx.git, "add_detached_worktree", add)
    with pytest.raises(GitError) as raised:
        if action == "merge":
            orch.merge(c.ctx, c.pr, approved_by="jin")
        else:
            orch.premerge(c.ctx, c.pr)
    assert raised.value is creation_error
    assert attempts == failed_attempt
    assert not c.tracker.merged_prs
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_merge_without_ci_uses_clock_and_records_pr_in_event(merge_case):
    c = merge_case
    c.tracker.set_pr_checks(c.pr, "none")
    orch.merge(c.ctx, c.pr, approved_by="jin")
    assert c.tracker.merged_prs == [c.pr]
    assert [comment.body for comment in c.tracker.list_comments(c.pr)] == [
        f"Approved by jin in the orchestrator ({BASE_TIME.isoformat()}). "
        "Pre-merge: tests pass, no conflicts, CI none."
    ]
    event = json.loads((c.ctx.store.root / "orch-events.jsonl").read_text())
    assert event["kind"] == "merge" and event["issue"] is None
    assert f"PR #{c.pr}" in event["summary"] and c.head in event["summary"]


@pytest.mark.parametrize("approval,at", [("  ", None), ("jin", "invalid-time")])
def test_invalid_attribution_is_rejected_before_checks(merge_case, approval, at):
    c = merge_case
    with pytest.raises(orch.OrchError):
        orch.merge(c.ctx, c.pr, approved_by=approval, at=at)
    assert not c.tracker.comments and not c.tracker.merged_prs

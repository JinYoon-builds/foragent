"""Saved source conflicts must use the resolver without losing reapproved locks."""

from dataclasses import replace

import pytest

from conftest import ACCEPTANCE, git_command
from runner_support import batch_setup, import_index, run_case  # noqa: F401
from foragent.git import GitRepo
from foragent.reasons import Reason
from foragent.runner import run_issue


@pytest.mark.parametrize("saved_at", ["local", "origin"])
def test_stale_batch_source_conflict_reaches_resolver_with_approved_locks(
    run_case, monkeypatch, saved_at,
):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, max_retries=0, skill_libraries=[], test_command="true")
    source = "src/widget.py"
    c.history.update({source: "VALUE = 'original'\n"})
    c.ctx.git.fetch("origin", "main")
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/batch-99", c.history.tip)
    branch, path = "foragent/issue-1", c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, branch, "origin/main")
    own = "VALUE = 'saved implementation'\n"
    (path / source).write_text(own)
    git_command(path, "add", source)
    import_index(path, "Saved implementation")
    saved = git_command(path, "rev-parse", "HEAD")
    c.ctx.git.remove_worktree(path)
    if saved_at == "origin":
        git_command(c.history.origin, "fetch", str(c.history.clone), f"{saved}:refs/heads/{branch}")
        git_command(c.history.clone, "update-ref", "-d", f"refs/heads/{branch}")
    approved = "def test_feature():\n    assert 1 + 1 == 2\n"
    c.history.update({source: "VALUE = 'base update'\n", ACCEPTANCE: approved})
    c.ctx.git.fetch("origin", "main")
    approval = c.store.load_approval(1)
    c.store.save_approval(
        1, body=approval.body, body_sha=approval.body_sha,
        acceptance_blobs=c.ctx.git.blob_shas("origin/main", [ACCEPTANCE]),
        approved_by=approval.approved_by, approved_at=c.now[0].isoformat(),
    )
    c.executor.steps = [{".foragent/QUESTION.md": "Fixture stopped at the resolver boundary."}]
    execute, observed = c.executor.run, []

    def observe(**kwargs):
        root = kwargs["worktree"]
        observed.append(root)
        assert c.store.get_run(1).phase == "resolving-conflict"
        assert (root / ACCEPTANCE).read_text() == approved
        assert git_command(root, "show", f"HEAD:{source}") == own.strip()
        assert GitRepo(root).conflict_paths() == [source]
        assert GitRepo(root).merge_in_progress()
        assert git_command(root, "rev-parse", "MERGE_HEAD") == c.history.tip
        assert "Resolve the conflict markers" in kwargs["prompt"]
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", observe)
    assert run_issue(1, c.ctx) == "spec-question"
    assert len(observed) == len(c.executor.calls) == 1
    assert c.store.load_approval(1) is not None
    assert c.store.load_flag(1).reason == Reason.SPEC_QUESTION

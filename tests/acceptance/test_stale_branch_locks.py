"""Acceptance test for stale-branch-locks. Locked by Foragent; do not edit."""

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from foragent.issue import body_hash
from foragent.reasons import Reason
from foragent.runner import run_issue


BRANCH = "foragent/issue-1"
BATCH = "foragent/batch-99"
LOCKS = (
    "tests/acceptance/test_role_signal_not_now.py",
    "tests/acceptance/test_role_signal_raise.py",
)
OLD = {path: f"# Earlier publication {i}\n" for i, path in enumerate(LOCKS)}
APPROVED = {path: f"# Corrected publication {i}\n" for i, path in enumerate(LOCKS)}


@pytest.fixture(autouse=True)
def fixture_git_only(tmp_path, monkeypatch):
    """Use real Git only in disposable repositories; all external services are fakes."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        assert not {"commit", "push", "reset", "checkout", "stash"}.intersection(argv), argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)


def issue_commit(case, parent, files):
    """Seed an issue's own history without running git commit or pushing anything."""
    stream = [
        f"commit refs/heads/{BRANCH}\n",
        "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
        f"data 10\nIssue work\nfrom {parent}\n",
    ]
    for path, content in files.items():
        stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(case.history.origin, "rev-parse", BRANCH)


def prepare(case, saved_at, review, *, own_files=None):
    case.ctx.config = replace(
        case.ctx.config, review=review, max_retries=0, skill_libraries=[],
        test_command="true", checks="local",
    )
    case.history.update(OLD)
    earlier = case.history.tip
    # A previous run created both refs before the order was corrected.
    git_command(case.history.origin, "update-ref", f"refs/heads/{BATCH}", earlier)
    issue_head = issue_commit(case, earlier, own_files) if own_files else earlier
    if saved_at == "local":
        case.ctx.git.fetch("origin", "main")
        if own_files:
            case.ctx.git.fetch("origin", BRANCH)
        git_command(case.history.clone, "update-ref", f"refs/heads/{BRANCH}", issue_head)
        git_command(case.history.origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
    else:
        git_command(case.history.origin, "update-ref", f"refs/heads/{BRANCH}", issue_head)
    case.tracker.add_issue(99, "Republished order")
    case.tracker.set_parent(1, 99)
    case.history.update(APPROVED)
    case.ctx.git.fetch("origin", "main")
    base = case.history.tip
    body = (
        "<!-- foragent:v1 -->\n## Goal\nRun the approved task.\n"
        "## Out of scope\nOther tasks.\n## Done when\n```sh\n"
        f"uv run pytest -q {' '.join(LOCKS)}\n```\n"
    )
    case.tracker.update_issue(1, title="Run the approved task", body=body)
    case.store.save_approval(
        1, body=body, body_sha=body_hash(body),
        acceptance_blobs=case.ctx.git.blob_shas("origin/main", LOCKS),
        approved_by="human", approved_at=case.now[0].isoformat(),
    )
    assert git_command(case.history.origin, "rev-list", "--count", f"{base}..{issue_head}") == (
        "1" if own_files else "0"
    )
    assert case.ctx.git.blob_shas(issue_head, LOCKS) != case.store.load_approval(1).acceptance_blobs
    # Stop after observing the public executor boundary; publication is outside this contract.
    case.executor.steps = [{".foragent/QUESTION.md": "Fixture stopped after implementation started."}]
    return base


def proceeds_on_approved_base(case, base, monkeypatch, *, preserved=None):
    observed = []
    execute = case.executor.run

    def observe(**kwargs):
        tree = kwargs["worktree"]
        observed.append({path: (tree / path).read_text() for path in LOCKS})
        assert observed[-1] == APPROVED, "The implementer must receive the approved locked bytes"
        git_command(tree, "merge-base", "--is-ancestor", base, "HEAD")
        for path, content in (preserved or {}).items():
            assert (tree / path).read_text() == content, "Keep the branch's own implementation work"
        return execute(**kwargs)

    monkeypatch.setattr(case.executor, "run", observe)
    outcome = run_issue(1, case.ctx)
    assert outcome == "spec-question", (
        f"An inherited lock difference must reach the fake implementer; got {outcome}. "
        f"flag={case.store.load_flag(1)}"
    )
    assert observed == [APPROVED], "Start implementation exactly once with approved files"
    assert len(case.executor.calls) == 1
    approval = case.store.load_approval(1)
    assert approval is not None, "Inherited base changes must keep the approval valid"
    assert approval.acceptance_blobs == case.ctx.git.blob_shas("origin/main", LOCKS)
    assert case.store.load_flag(1).reason == Reason.SPEC_QUESTION


@pytest.mark.parametrize("saved_at", ["local", "origin"])
@pytest.mark.parametrize("review", ["fast", "batch"])
def test_branch_without_own_commits_starts_with_approved_base(run_case, monkeypatch, saved_at, review):
    base = prepare(run_case, saved_at, review)
    proceeds_on_approved_base(run_case, base, monkeypatch)


@pytest.mark.parametrize("saved_at", ["local", "origin"])
def test_inherited_lock_update_preserves_own_source_commit(run_case, monkeypatch, saved_at):
    own = {"src/widget.py": "VALUE = 'saved implementation'\n"}
    base = prepare(run_case, saved_at, "batch", own_files=own)
    proceeds_on_approved_base(run_case, base, monkeypatch, preserved=own)


@pytest.mark.parametrize("saved_at", ["local", "origin"])
@pytest.mark.parametrize("edited_lock", LOCKS)
def test_own_commit_editing_a_locked_file_still_invalidates(run_case, saved_at, edited_lock):
    prepare(run_case, saved_at, "batch", own_files={edited_lock: "# Issue edited its locked test\n"})
    outcome = run_issue(1, run_case.ctx)
    assert outcome == "hash-mismatch", "An own locked edit must invalidate approval before execution"
    assert run_case.store.load_approval(1) is None
    assert run_case.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert "approved" not in run_case.tracker.get_issue(1).labels
    assert run_case.executor.calls == []
    assert run_case.tracker.created_prs == []

"""Standalone options and error paths beyond the fixed branch review acceptance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import review_branch, run_review
from foragent.commands import review as command
from foragent.config import Config
from foragent.git import GitError
from foragent.run_review import ReviewResult
from foragent.state import StateStore
from foragent.tracker import GhTracker


def test_goal_is_verbatim_and_does_not_require_an_issue(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("A direct Goal must not read issue state")

    monkeypatch.setattr(StateStore, "for_repo", forbidden)
    for goal in (None, "Keep uploads bounded.\nRetain diagnostics."):
        parsed = review_branch.goal_for(tmp_path, Config(), goal, None)
        assert parsed.goal == (goal or "")
        assert parsed.out_of_scope == ""


@pytest.mark.parametrize("cached", [True, False])
def test_issue_grounding_reads_cache_before_github(tmp_path, monkeypatch, cached):
    """--issue reads only the local issue cache; a cache miss stops plainly and never calls GitHub."""
    body = "## Goal\nBound retries.\n\n## Out of scope\nTransport changes.\n\n## Done when\n```sh\nfalse\n```\n"
    rows = [{"number": 42, "body": body}] if cached else [{"number": 7, "body": body}]
    monkeypatch.setattr(StateStore, "local_issues", lambda self: rows)
    calls = []

    def get_issue(self, number):
        calls.append((self.repo, number))
        return SimpleNamespace(body=body)

    monkeypatch.setattr(GhTracker, "get_issue", get_issue)
    if cached:
        parsed = review_branch.goal_for(tmp_path, Config(repo="owner/repo"), None, 42)
        assert parsed.goal == "Bound retries."
        assert parsed.out_of_scope == "Transport changes."
        assert parsed.done_when_commands == []
    else:
        with pytest.raises(review_branch.ReviewError, match="#42"):
            review_branch.goal_for(tmp_path, Config(repo="owner/repo"), None, 42)
    assert calls == []
    assert not any(tmp_path.iterdir())


def test_standalone_finish_scores_once_without_run_journal(monkeypatch):
    calls = []
    original = ReviewResult("FAIL", "src/upload.py:3: retry loops")
    scored = ReviewResult("PASS", "", review_notes=("not grounded",))

    def score(n, ctx, review, round_number, **kwargs):
        calls.append((n, review.findings, round_number, kwargs))
        return scored

    def forbidden(*args, **kwargs):
        pytest.fail("Standalone finish must not journal or update run state")

    monkeypatch.setattr(run_review.review_scoring, "score", score)
    monkeypatch.setattr(run_review, "_journal_review", forbidden)
    monkeypatch.setattr(run_review, "_record_notes", forbidden)
    result = run_review._finish_review(0, object(), original, 1, ("VERDICT: FAIL",),
                                       git="git", parsed="parsed", journal=False)
    assert result is scored
    assert calls == [(0, original.findings, 1, {"git": "git", "parsed": "parsed", "record": False})]


@pytest.mark.parametrize("result,code", [
    (ReviewResult("UNUSABLE", "", notes=("missing verdict",)), 2),
    (ReviewResult("PASS", "", conflicts=("SPEC-CONFLICT: unresolved Goal",)), 2),
    (ReviewResult("FAIL", "finding", execution_failed=True), 2),
])
def test_errors_are_not_pass_or_fail(monkeypatch, capsys, result, code):
    monkeypatch.setattr(review_branch, "review", lambda *args, **kwargs: (result, "en", False))
    args = SimpleNamespace(target="feature", base="main", goal=None, issue=None)
    assert command.run(args) == code
    out = capsys.readouterr().out
    assert f"VERDICT: {result.verdict}" in out


@pytest.mark.parametrize("failed_cleanup", ["worktree", "directory", "both"])
def test_cleanup_failures_preserve_the_original_review_error(tmp_path, monkeypatch, failed_cleanup):
    original = ValueError("panel original error")
    real_temporary = review_branch.TemporaryDirectory
    paths, removed = [], []

    class Directory:
        def __init__(self, **kwargs):
            self.inner = real_temporary(**kwargs)
            self.name = self.inner.name
            paths.append(self.inner.name)

        def cleanup(self):
            self.inner.cleanup()
            if failed_cleanup in {"directory", "both"}:
                raise OSError("directory cleanup failed")

    class Git:
        def __init__(self, root):
            self.root = root

        def _call(self, args):
            if args == ["rev-parse", "--show-toplevel"]:
                return str(tmp_path)
            if args[:2] == ["worktree", "remove"]:
                removed.append(args[-1])
                if failed_cleanup in {"worktree", "both"}:
                    raise GitError("worktree cleanup failed")
            return "commit"

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(review_branch, "GitRepo", Git)
    monkeypatch.setattr(review_branch, "TemporaryDirectory", Directory)
    monkeypatch.setattr(review_branch, "resolve_target", lambda *args: ("head", False))
    monkeypatch.setattr(review_branch, "load_config", lambda *args: Config())
    monkeypatch.setattr(review_branch, "_panel_review", fail)
    with pytest.raises(ValueError) as caught:
        review_branch.review("feature")
    assert caught.value is original
    assert len(removed) == 1
    assert len(paths) == 1
    assert not Path(paths[0]).exists()
    notes = " ".join(original.__notes__)
    assert ("worktree cleanup failed" in notes) == (failed_cleanup in {"worktree", "both"})
    assert ("directory cleanup failed" in notes) == (failed_cleanup in {"directory", "both"})


def test_a_cleanup_failure_after_success_is_still_an_error():
    calls = []

    def fail():
        calls.append("cleanup")
        raise OSError("cleanup only")

    with pytest.raises(OSError, match="cleanup only"):
        with review_branch._cleanup(fail):
            pass
    assert calls == ["cleanup"]


def test_cli_prints_original_error_and_cleanup_diagnostic(monkeypatch, capsys):
    original = ValueError("panel original error")
    original.add_note("Cleanup also failed: worktree cleanup failed")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(review_branch, "review", fail)
    args = SimpleNamespace(target="feature", base="main", goal=None, issue=None)
    assert command.run(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "panel original error" in captured.err
    assert "worktree cleanup failed" in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.parametrize("target_kind", ["branch", "worktree"])
def test_review_runs_no_program_the_repository_configures(tmp_path, monkeypatch, target_kind):
    """Hooks, fsmonitor and filters configured in the person's repository never run during `fa review`."""
    import os
    import subprocess

    repo, log = tmp_path / "repo", tmp_path / "repo" / "side-effects.log"
    git = lambda *args, cwd=repo: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
    repo.mkdir()
    git("init", "-q", "-b", "main")
    env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}

    def commit(ref, message, parent=None):
        # Plumbing commits: the shared test guard forbids `git commit`.
        git("add", "-A")
        tree = git("write-tree").strip()
        sha = subprocess.run(["git", "commit-tree", tree, "-m", message, *(["-p", parent] if parent else [])], cwd=repo,
                             check=True, capture_output=True, text=True, env={**os.environ, **env}).stdout.strip()
        git("update-ref", ref, sha)
        return sha

    (repo / ".gitattributes").write_text("*.txt filter=mark\n")
    (repo / "a.txt").write_text("base\n")
    (repo / ".gitignore").write_text("side-effects.log\n")
    base = commit("refs/heads/main", "base")
    (repo / "a.txt").write_text("feature\n")
    commit("refs/heads/feature", "feature", base)
    (repo / "a.txt").write_text("base\n")
    git("read-tree", "main")
    target = "feature"
    if target_kind == "worktree":
        target = str(tmp_path / "feature-tree")
        git("worktree", "add", "-q", target, "feature")
    for hook in ("post-checkout", "reference-transaction", "pre-commit", "post-index-change"):
        path = repo / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\necho {hook} >> '{log}'\n")
        path.chmod(0o755)
    record = lambda what: f"sh -c 'echo {what} >> \"{log}\"; cat'"
    git("config", "filter.mark.smudge", record("smudge"))
    git("config", "filter.mark.clean", record("clean"))
    git("config", "core.fsmonitor", f"{repo}/.git/hooks/post-checkout")
    before = dict(os.environ)
    seen = {}

    def panel(n, ctx, checkout, *args, **kwargs):
        state = checkout.review_state()
        checkout._call(["update-ref", "HEAD", checkout.head_sha()])
        (checkout.root / "a.txt").write_text("edited by a reviewer\n")
        checkout._call(["add", "-A"])
        checkout.restore_review_state(state)
        seen["file"] = (checkout.root / "a.txt").read_text()
        return ReviewResult("PASS", "")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(review_branch, "_panel_review", panel)
    monkeypatch.setattr(review_branch, "load_config", lambda root: Config())
    result, _language, _worktree = review_branch.review(target)
    assert result.verdict == "PASS"
    assert seen["file"] == "feature\n", "the checkout holds the branch's blob, unfiltered"
    assert not log.exists() or log.read_text() == "", f"repository programs ran: {log.read_text()!r}"
    assert dict(os.environ) == before

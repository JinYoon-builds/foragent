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
    body = "## Goal\nBound retries.\n\n## Out of scope\nTransport changes.\n\n## Done when\n```sh\nfalse\n```\n"
    rows = [{"number": 42, "body": body}] if cached else []
    monkeypatch.setattr(StateStore, "local_issues", lambda self: rows)
    calls = []

    def get_issue(self, number):
        calls.append((self.repo, number))
        return SimpleNamespace(body=body)

    monkeypatch.setattr(GhTracker, "get_issue", get_issue)
    parsed = review_branch.goal_for(tmp_path, Config(repo="owner/repo"), None, 42)
    assert parsed.goal == "Bound retries."
    assert parsed.out_of_scope == "Transport changes."
    assert parsed.done_when_commands == []
    assert calls == ([] if cached else [("owner/repo", 42)])
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

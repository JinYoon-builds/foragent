import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from foragent.git import GitError, GitRepo

from conftest import ACCEPTANCE, BASE_TIME, git_command


@pytest.mark.parametrize("url", [
    "git@github.com:owner/repo.git",
    "https://github.com/owner/repo",
    "https://github.com/owner/repo.git",
    "ssh://git@github.com/owner/repo.git",
])
def test_remote_slug_forms(tmp_path, url):
    calls = []

    def run(args):
        calls.append(args)
        return url + "\n"

    assert GitRepo(tmp_path, run).remote_slug() == "owner/repo"
    assert calls == [["remote", "get-url", "origin"]]


def test_remote_slug_custom_remote_and_unsupported_url(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        return "/local/origin.git\n"

    with pytest.raises(GitError, match="set repo"):
        GitRepo(tmp_path, run).remote_slug("upstream")
    assert calls == [["remote", "get-url", "upstream"]]


@pytest.mark.parametrize("branch", ["main", "trunk", "release/v1"])
def test_push_base_uses_explicit_source_and_destination_despite_push_config(tmp_path, branch):
    calls = []
    git = GitRepo(tmp_path, lambda args: calls.append(args) or "")
    git.push_base(branch)
    assert calls == [["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"]]


def test_setup_status_preserves_leading_status_columns_and_nul_paths(tmp_path):
    calls = []
    git = GitRepo(tmp_path, lambda args: calls.append(args) or " M .foragent.yml\0A  odd\nname\0")
    assert git.uncommitted_paths([".foragent.yml", "odd\nname"]) == [".foragent.yml", "odd\nname"]
    assert calls == [[
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", ".foragent.yml", "odd\nname",
    ]]


def test_blobs_present_missing_and_invalid_ref(git_history):
    git = GitRepo(git_history.clone)
    sha = git.blob_sha("origin/main", ACCEPTANCE)
    assert sha is not None and len(sha) == 40
    assert git.blob_sha("origin/main", "absent.py") is None
    assert git.blob_shas("origin/main", [ACCEPTANCE, "absent.py"]) == {
        ACCEPTANCE: sha, "absent.py": "missing",
    }
    with pytest.raises(GitError):
        git.blob_sha("not-a-ref", ACCEPTANCE)


@pytest.mark.parametrize("custom_refspec", [False, True])
def test_fetch_updates_remote_tracking_blobs(git_history, custom_refspec):
    git = GitRepo(git_history.clone)
    if custom_refspec:
        git_command(
            git_history.clone, "config", "remote.origin.fetch",
            "+refs/heads/other:refs/remotes/origin/other",
        )
    before = git.blob_sha("origin/main", ACCEPTANCE)
    git_history.update({ACCEPTANCE: "def test_feature():\n    assert 1 == 1\n"})
    assert git.blob_sha("origin/main", ACCEPTANCE) == before
    git.fetch("origin", "main")
    assert git.blob_sha("origin/main", ACCEPTANCE) != before


def test_commit_before_selects_approval_time_history(git_history):
    git = GitRepo(git_history.clone)
    initial = git_history.tip
    approval_time = datetime(2026, 10, 20, 9, tzinfo=UTC)
    git_history.update({ACCEPTANCE: "pass\n"}, at=approval_time + timedelta(minutes=1))
    git.fetch("origin", "main")
    assert git.commit_before("origin/main", approval_time.isoformat()) == initial
    assert git.commit_before("origin/main", (approval_time + timedelta(minutes=2)).isoformat()) == git_history.tip
    assert git.commit_before("origin/main", (BASE_TIME - timedelta(seconds=1)).isoformat()) is None


def test_commit_before_passes_timestamp_as_one_argument(tmp_path):
    calls = []
    git = GitRepo(tmp_path, lambda args: calls.append(args) or "abc123\n")
    assert git.commit_before("origin/main", "2026-10-20T09:00:00+09:00") == "abc123"
    assert calls == [["rev-list", "-1", "--before=2026-10-20T09:00:00+09:00", "origin/main"]]


def test_injected_fetch_and_default_remote(tmp_path):
    calls = []
    git = GitRepo(tmp_path, lambda args: calls.append(args) or "")
    git.fetch()
    git.fetch("upstream", "trunk")
    git.fetch("origin", "release/v1")
    assert calls == [
        ["fetch", "origin"],
        ["fetch", "upstream", "+refs/heads/trunk:refs/remotes/upstream/trunk"],
        ["fetch", "origin", "+refs/heads/release/v1:refs/remotes/origin/release/v1"],
    ]


@pytest.mark.parametrize("failure", [
    FileNotFoundError("git not found"),
    subprocess.CalledProcessError(128, ["git"], stderr="remote unavailable"),
])
def test_git_errors_are_wrapped(tmp_path, failure):
    def run(args):
        raise failure

    with pytest.raises(GitError, match="git not found|remote unavailable"):
        GitRepo(tmp_path, run).fetch()


@pytest.mark.parametrize("args,timeout", [
    (["rev-parse", "HEAD"], 120),
    (["diff", "--cached", "--name-only", "HEAD"], 120),
    (["fetch", "origin"], 300),
    (["push", "origin", "main"], 300),
    (["-C", "worktree", "fetch", "origin"], 300),
    (["-C", "worktree", "push", "origin", "main"], 300),
    (["-C", "outer", "-C", "inner", "push", "origin", "main"], 300),
    (["show", "HEAD:push"], 120),
])
@pytest.mark.parametrize("expired", [False, True])
def test_default_git_runner_bounds_all_commands_and_wraps_timeouts(tmp_path, monkeypatch, args, timeout, expired):
    def run(command, **kwargs):
        assert command == ["git", *args]
        assert kwargs == {
            "cwd": tmp_path, "check": True, "capture_output": True, "text": True, "timeout": timeout,
        }
        if expired:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, stdout="result\n")

    monkeypatch.setattr("foragent.git.subprocess.run", run)
    git = GitRepo(tmp_path)
    if expired:
        with pytest.raises(GitError, match=f"timed out after {timeout} seconds") as error:
            git._call(args)
        assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    else:
        assert git._call(args) == "result\n"


def test_runner_git_commands_are_exact_and_injectable(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if "diff" in args:
            return "a.py\0odd\nname.py\0"
        return ""

    git = GitRepo(tmp_path, run)
    path = tmp_path / "worktree"
    git.add_worktree(path, "foragent/issue-1", "origin/main")
    worktree = git.at(path)
    worktree.stage_changes()
    assert worktree.changed_paths("origin/main") == ["a.py", "odd\nname.py"]
    worktree.commit("Issue title (#1)")
    worktree.push("foragent/issue-1")
    path.mkdir()
    git.remove_worktree(path)
    assert calls == [
        ["worktree", "add", "-B", "foragent/issue-1", str(path), "origin/main"],
        ["-C", str(path), "add", "-A", "--", ".", ":!.foragent"],
        ["-C", str(path), "diff", "--cached", "--name-only", "--no-renames", "-z", "origin/main"],
        ["-C", str(path), "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
         "commit", "-m", "Issue title (#1)"],
        ["-C", str(path), "push", "-f", "origin", "foragent/issue-1"],
        ["worktree", "remove", "--force", str(path)],
        ["worktree", "prune"],
    ]

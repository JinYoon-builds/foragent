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


def test_ensure_tracked_includes_ignored_files_without_staging_unrelated_changes(git_history):
    root = git_history.clone
    git = GitRepo(root)
    (root / ".gitignore").write_text("tests/acceptance/\n")
    name = "tests/acceptance/ignored test.py"
    (root / name).write_text("assert True\n")
    (root / ACCEPTANCE).write_text("unrelated local edit\n")
    assert git.uncommitted_paths([name]) == []
    git.ensure_tracked([name, ACCEPTANCE])
    assert git_command(root, "ls-files", "--", name) == name
    assert git_command(root, "show", f":{name}") == "assert True"
    assert git.changed_paths("HEAD") == [name]


def test_ensure_tracked_rejects_files_still_missing_after_force_add(tmp_path):
    calls = []
    git = GitRepo(tmp_path, lambda args: calls.append(args) or "")
    git.ensure_tracked([])
    assert calls == []
    with pytest.raises(GitError, match="not tracked: ignored.py"):
        git.ensure_tracked(["ignored.py"])
    assert calls == [
        ["ls-files", "--cached", "-z", "--", "ignored.py"],
        ["add", "-f", "--", "ignored.py"],
        ["ls-files", "--cached", "-z", "--", "ignored.py"],
    ]


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


@pytest.mark.parametrize("replacement", ["ordinary", "file-to-directory", "directory-to-file", "directory-to-symlink", "unmerged"])
def test_review_restore_preserves_index_unstaged_files_modes_and_symlinks(git_history, replacement):
    root = git_history.clone
    git = GitRepo(root)
    feature = root / "feature.txt"
    feature.write_text("staged implementation")
    directory = root / "directory"
    directory.mkdir()
    (directory / "child.txt").write_text("original child")
    (root / "link").symlink_to("feature.txt")
    git.stage_changes()
    feature.write_text("original unstaged content")
    (root / ".foragent").mkdir()
    (root / ".foragent/keep").write_text("existing metadata")
    before = git.review_state()
    feature.write_text("reviewer content")
    (root / ACCEPTANCE).chmod(0o755)
    (root / "link").unlink()
    (root / "link").symlink_to("directory/child.txt")
    (root / ".foragent/keep").write_text("reviewer metadata")
    (root / "extra.txt").write_text("reviewer addition")
    git.stage_changes()
    if replacement == "file-to-directory":
        feature.unlink()
        feature.mkdir()
        (feature / "new.txt").write_text("reviewer nested addition")
    elif replacement in {"directory-to-file", "directory-to-symlink"}:
        (directory / "child.txt").unlink()
        directory.rmdir()
        if replacement == "directory-to-file":
            directory.write_text("reviewer file")
        else:
            external = root.parent / "external"
            external.mkdir()
            (external / "child.txt").write_text("external content must survive")
            directory.symlink_to(external)
    elif replacement == "unmerged":
        sha = git.blob_sha("HEAD", ACCEPTANCE)
        git_command(root, "update-index", "--index-info", input=f"0 {'0' * 40}\tfeature.txt\n100644 {sha} 1\tfeature.txt\n")
    assert git.review_state() != before
    git.restore_review_state(before)
    assert git.review_state() == before
    assert git_command(root, "show", ":feature.txt") == "staged implementation"
    assert feature.read_text() == "original unstaged content"
    if replacement == "directory-to-symlink":
        assert (external / "child.txt").read_text() == "external content must survive"


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
            "errors": "replace",
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
        ["-C", str(path), "rm", "--cached", "-r", "-f", "--ignore-unmatch", ".foragent"],
        ["-C", str(path), "diff", "--cached", "--name-only", "--no-renames", "-z", "origin/main"],
        ["-C", str(path), "rm", "--cached", "-r", "-f", "--ignore-unmatch", ".foragent"],
        ["-C", str(path), "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
         "commit", "-m", "Issue title (#1)"],
        ["-C", str(path), "push", "-f", "origin", "foragent/issue-1"],
        ["worktree", "remove", "--force", str(path)],
        ["worktree", "prune"],
    ]


@pytest.mark.parametrize("method", ["stage_changes", "commit", "commit_paths"])
def test_all_staging_and_commit_paths_remove_prestaged_artifacts(git_history, method):
    root = git_history.clone
    files = {".foragent/TRY.md": "try card", ".foragent/nested/session.log": "session",
             "feature.txt": "implementation", ".foragent.yml": "review: fast\n"}
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    git_command(root, "add", "-A")
    # Force exclusion even if an artifact changed after it was staged.
    (root / ".foragent/TRY.md").write_text("updated try card")
    commits = []

    def run(args):
        if "commit" in args:
            commits.append(args)
            assert git_command(root, "ls-files", "--", ".foragent") == ""
            return ""
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    git = GitRepo(root, run)
    if method == "commit_paths":
        git.commit_paths(["feature.txt"], "Implementation")
    elif method == "commit":
        git.commit("Implementation")
    else:
        git.stage_changes()
    assert len(commits) == (method != "stage_changes")
    assert git.changed_paths("HEAD") == [".foragent.yml", "feature.txt"]
    assert (root / ".foragent/TRY.md").read_text() == "updated try card"
    assert (root / ".foragent/nested/session.log").read_text() == "session"


def test_review_baseline_restores_head_even_when_index_and_files_are_unchanged(git_history):
    git = GitRepo(git_history.clone)
    feature = git.root / "feature.txt"
    feature.write_text("implementation")
    git.stage_changes()
    feature.write_text("unstaged implementation")
    before = git.review_state()
    git_history.update({"other.txt": "reviewer commit"})
    git.fetch("origin", "main")
    git_command(git.root, "update-ref", "HEAD", git_history.tip)
    after = git.review_state()
    assert after.staged_tree == before.staged_tree and after.files == before.files
    assert after.head_sha != before.head_sha
    git.restore_review_state(before)
    assert git.review_state() == before
    assert git_command(git.root, "show", ":feature.txt") == "implementation"
    assert feature.read_text() == "unstaged implementation"


def test_non_utf8_diff_decodes_with_replacement_and_patch_keeps_original_bytes(git_history, tmp_path):
    git = GitRepo(git_history.clone)
    (git.root / "latin1.txt").write_bytes(b"caf\xe9\n")
    git.stage_changes()
    assert "caf\ufffd" in git.staged_diff("HEAD")
    patch = tmp_path / "child.patch"
    git.write_staged_patch("HEAD", patch)
    assert b"caf\xe9\n" in patch.read_bytes()


@pytest.mark.parametrize("attribute,width", [
    ("", 7),
    ("conflict-marker-size=5", 5),
    ("conflict-marker-size=12", 12),
    ("conflict-marker-size=0", 7),
    ("conflict-marker-size=invalid", 7),
    ("-conflict-marker-size", 7),
])
@pytest.mark.parametrize("marker", ["<", "|", "=", ">"])
def test_conflict_markers_use_path_attributes_and_include_ancestor(git_history, attribute, width, marker):
    root = git_history.clone
    if attribute:
        (root / ".gitattributes").write_text(f"*.txt {attribute}\n")
    name = "odd\nname.txt"
    (root / name).write_bytes(f"content\r\n{marker * width} ancestor\r\n".encode())
    assert GitRepo(root).conflict_marker_paths([name]) == [name]
    (root / name).write_text("resolved content\n")
    assert GitRepo(root).conflict_marker_paths([name]) == []


def test_conflict_marker_width_is_selected_per_file(git_history):
    root = git_history.clone
    (root / ".gitattributes").write_text("small.txt conflict-marker-size=5\nwide.txt conflict-marker-size=12\n")
    (root / "small.txt").write_text("<<<<< HEAD\n")
    (root / "wide.txt").write_text("|||||||||||| ancestor\n")
    assert GitRepo(root).conflict_marker_paths(["wide.txt", "small.txt", "small.txt"]) == ["small.txt", "wide.txt"]

"""Boundary cases for classifying a moved base (#376)."""

from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from foragent.git import GitRepo
from foragent.run_baseline import AcceptanceBaseline
from foragent.run_base_move import base_move_is_neutral, moved_paths


def git(root: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env=None if env is None else {**os.environ, **env}).stdout.strip()


def commit(root: Path, branch: str, files: dict[str, str | None], *, parents: tuple[str, ...] = ()) -> str:
    """Add one commit to branch with plumbing only; a guard forbids porcelain commits."""
    index = {"GIT_INDEX_FILE": str(root.parent / f"index-{branch}")}
    tip = git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}") if branch_exists(root, branch) else None
    if tip:
        git(root, "read-tree", tip, env=index)
    else:
        git(root, "read-tree", "--empty", env=index)
    for name, text in files.items():
        if text is None:
            git(root, "update-index", "--force-remove", name, env=index)
            continue
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input=text,
                              check=True, capture_output=True, text=True).stdout.strip()
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=index)
    tree = git(root, "write-tree", env=index)
    parent_args = [arg for parent in ((tip,) if tip else ()) + parents for arg in ("-p", parent)]
    sha = git(root, "commit-tree", tree, *parent_args, "-m", "change")
    git(root, "update-ref", f"refs/heads/{branch}", sha)
    return sha


def branch_exists(root: Path, branch: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=root,
                          capture_output=True).returncode == 0


def move(root: Path, files: dict[str, str | None]) -> str:
    return commit(root, "main", files)


def on_feature(root: Path) -> None:
    git(root, "symbolic-ref", "HEAD", "refs/heads/feature")
    git(root, "read-tree", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    base = move(root, {"src/a.py": "A = 0\n", "docs/a.md": "a\n", "tests/acceptance/test_other.py": "x\n"})
    git(root, "update-ref", "refs/heads/feature", base)
    on_feature(root)
    return root


OTHER = "tests/acceptance/test_other.py"


def attempt(root: Path, owners: dict[int, list[str]], *, merged=(), n: int = 1, skipped=(OTHER,)):
    approvals = {number: SimpleNamespace(number=number, acceptance_blobs={name: "sha" for name in names})
                 for number, names in owners.items()}
    state = root.parent / "state"
    (state / "approvals").mkdir(parents=True, exist_ok=True)
    for number in approvals:
        (state / "approvals" / f"{number}.json").write_text("{}")
    store = SimpleNamespace(
        root=state, load_approval=approvals.get,
        read_history=lambda: [SimpleNamespace(number=number, outcome="merged") for number in merged],
    )
    baseline = AcceptanceBaseline("pytest", (), excluded=tuple(skipped))
    return SimpleNamespace(n=n, ctx=SimpleNamespace(store=store), git=GitRepo(root), baseline=baseline)


def test_every_intervening_commit_counts_even_when_reverted(repo):
    move(repo, {"src/a.py": "A = 1\n"})
    target = move(repo, {"src/a.py": "A = 0\n", "docs/b.md": "b\n"})
    assert git(repo, "diff", "--name-only", "feature", target) == "docs/b.md"
    assert moved_paths(attempt(repo, {}), target) == {"src/a.py", "docs/b.md"}
    assert not base_move_is_neutral(attempt(repo, {}), target)


def test_docs_only_move_is_neutral(repo):
    move(repo, {"docs/b.md": "b\n"})
    target = move(repo, {"docs/specs/c.md": "c\n", "docs/a.md": None})
    assert base_move_is_neutral(attempt(repo, {}), target)


def test_rename_out_of_docs_is_result_affecting(repo):
    target = move(repo, {"docs/a.md": None, "src/a.md": "a\n"})
    assert moved_paths(attempt(repo, {}), target) == {"docs/a.md", "src/a.md"}
    assert not base_move_is_neutral(attempt(repo, {}), target)


def test_merge_commit_on_base_is_inspected_against_each_parent(repo):
    git(repo, "update-ref", "refs/heads/side", git(repo, "rev-parse", "main"))
    side = commit(repo, "side", {"src/a.py": "A = 2\n"})
    move(repo, {"docs/b.md": "b\n"})
    target = commit(repo, "main", {"src/a.py": "A = 2\n"}, parents=(side,))
    assert "src/a.py" in moved_paths(attempt(repo, {}), target)
    assert not base_move_is_neutral(attempt(repo, {}), target)


@pytest.mark.parametrize(("owners", "merged", "neutral"), [
    pytest.param({2: ["tests/acceptance/test_other.py"]}, (), True, id="other-open-owner"),
    pytest.param({2: ["tests/acceptance/test_other.py"]}, (2,), False, id="owner-merged"),
    pytest.param({1: ["tests/acceptance/test_other.py"]}, (), False, id="own-issue"),
    pytest.param({1: ["tests/acceptance/test_other.py"], 2: ["tests/acceptance/test_other.py"]}, (), False,
                 id="shared-with-own-issue"),
    pytest.param({}, (), False, id="no-owner"),
])
def test_locked_file_is_neutral_only_for_another_unmerged_owner(repo, owners, merged, neutral):
    target = move(repo, {"tests/acceptance/test_other.py": "y\n", "docs/c.md": "c\n"})
    assert base_move_is_neutral(attempt(repo, owners, merged=merged), target) is neutral


def test_docs_this_branch_changed_are_result_affecting(repo):
    commit(repo, "feature", {"docs/a.md": "feature\n"})
    on_feature(repo)
    other = move(repo, {"docs/b.md": "b\n"})
    target = move(repo, {"docs/a.md": "a\nmain\n"})
    assert base_move_is_neutral(attempt(repo, {}), other)
    assert not base_move_is_neutral(attempt(repo, {}), target)


def test_locked_file_the_baseline_ran_is_result_affecting_even_if_now_owned(repo):
    """An issue approved after verification does not undo what the baseline already ran."""
    target = move(repo, {OTHER: "y\n"})
    owners = {2: [OTHER]}
    assert base_move_is_neutral(attempt(repo, owners), target)
    assert not base_move_is_neutral(attempt(repo, owners, skipped=()), target)
    no_baseline = attempt(repo, owners)
    no_baseline.baseline = None
    assert not base_move_is_neutral(no_baseline, target)

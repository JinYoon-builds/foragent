"""Boundaries of the test-harness change detector."""

import pytest

from conftest import git_command
from foragent.git import GitRepo
from foragent.harness_guard import changed_harness_paths

PYPROJECT = '[project]\nname = "x"\nversion = "0"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git_command(root, "init", "-q", "--initial-branch=main")
    git_command(root, "config", "user.email", "t@example.com")
    git_command(root, "config", "user.name", "T")
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "tests").mkdir()
    (root / "tests/conftest.py").write_text("# base\n")
    commit(root, "refs/heads/main")
    git_command(root, "update-ref", "refs/heads/target", "HEAD")
    return root


def commit(root, ref, parent=None):
    """Plumbing-only commit of the working tree."""
    git_command(root, "add", "-A")
    tree = git_command(root, "write-tree").strip()
    sha = git_command(root, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", "c").strip()
    git_command(root, "update-ref", ref, sha)
    return sha


def staged(root):
    git_command(root, "add", "-A")
    return GitRepo(root)


def test_rename_reports_both_paths_and_listed_glob_is_excluded(repo):
    (repo / "tests/unit").mkdir()
    (repo / "tests/conftest.py").rename(repo / "tests/unit/conftest.py")
    git = staged(repo)
    assert changed_harness_paths(git, "target", []) == ["tests/conftest.py", "tests/unit/conftest.py"]
    assert changed_harness_paths(git, "target", ["tests/**"]) == []
    assert changed_harness_paths(git, "target", ["tests/unit/*.py"]) == ["tests/conftest.py"]


def test_target_side_harness_change_does_not_count(repo):
    (repo / "tests/conftest.py").write_text("# target moved\n")
    commit(repo, "refs/heads/target", parent=git_command(repo, "rev-parse", "HEAD").strip())
    (repo / "tests/conftest.py").write_text("# base\n")
    (repo / "feature.txt").write_text("x\n")
    assert changed_harness_paths(staged(repo), "target", []) == []


@pytest.mark.parametrize("text,held", [
    (PYPROJECT + 'dependencies = []\n', True),  # TOML: belongs to the ini_options table
    (PYPROJECT.replace('version = "0"', 'version = "1"'), False),
    (PYPROJECT + '\n[tool.pytest.ini_options.markers]\nslow = "x"\n', True),
    (PYPROJECT + '\n[tool.ruff]\nline-length = 1\n', False),
    (PYPROJECT + "\n[broken\n", True),
])
def test_pyproject_only_pytest_tables_and_unparsable_files_count(repo, text, held):
    (repo / "pyproject.toml").write_text(text)
    assert changed_harness_paths(staged(repo), "target", []) == (["pyproject.toml"] if held else [])


def test_added_nested_pyproject_with_pytest_table_and_deletion_count(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg/pyproject.toml").write_text('[tool.pytest]\naddopts = ["-q"]\n')
    (repo / "pyproject.toml").unlink()
    assert changed_harness_paths(staged(repo), "target", []) == ["pkg/pyproject.toml", "pyproject.toml"]

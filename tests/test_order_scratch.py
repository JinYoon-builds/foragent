"""order_scratch.changes: empty new folders are not changes, everything else keeps its rule."""

import os

from foragent import order_scratch
from foragent.git import GitRepo
from conftest import git_command


def _scratch(history, tmp_path, files=None):
    if files:
        history.update(files)
        git_command(history.clone, "pull", "--quiet", "origin", "main")
    git = GitRepo(history.clone)
    tree = tmp_path / "writer"
    tip = git_command(history.clone, "rev-parse", "HEAD")
    manifest = order_scratch.build(git, tip, tree)
    return git, tree, manifest


def _changes(git, tree, manifest):
    return order_scratch.changes(git, tree, manifest, "tests/acceptance")


def test_empty_nested_new_folders_are_not_changes(git_history, tmp_path):
    git, tree, manifest = _scratch(git_history, tmp_path)
    (tree / ".claude/.cc-writes").mkdir(parents=True)
    (tree / "deep/a/b/c").mkdir(parents=True)
    assert _changes(git, tree, manifest) == ({}, [], [])
    assert (tree / "deep/a/b/c").is_dir()


def test_new_folder_holding_a_file_is_refused_with_the_file(git_history, tmp_path):
    git, tree, manifest = _scratch(git_history, tmp_path)
    (tree / "tool/a").mkdir(parents=True)
    (tree / "tool/a/x.txt").write_text("x\n", encoding="utf-8")
    (tree / "tool/empty").mkdir()
    assert _changes(git, tree, manifest) == ({}, ["tool", "tool/a", "tool/a/x.txt"], [])


def test_new_folder_holding_only_a_symlink_is_refused(git_history, tmp_path):
    git, tree, manifest = _scratch(git_history, tmp_path)
    (tree / "tool").mkdir()
    os.symlink("nowhere", tree / "tool/link")
    assert _changes(git, tree, manifest) == ({}, ["tool", "tool/link"], [])


def test_existing_folder_re_permissioned_or_removed_stays_refused(git_history, tmp_path):
    git, tree, manifest = _scratch(git_history, tmp_path, {"docs/keep/a.md": "a\n", "other/b.md": "b\n"})
    (tree / "docs/keep").chmod(0o700)
    (tree / "other/b.md").unlink()
    (tree / "other").rmdir()
    _, refused, _ = _changes(git, tree, manifest)
    assert "docs/keep" in refused
    assert {"other", "other/b.md"} <= set(refused)


def test_empty_folder_in_an_ignored_new_directory_is_still_listed(git_history, tmp_path):
    git, tree, manifest = _scratch(git_history, tmp_path, {".gitignore": "cache/\n"})
    (tree / "cache/empty").mkdir(parents=True)
    assert _changes(git, tree, manifest) == ({}, [], ["cache"])

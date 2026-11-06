"""order_scratch.changes: empty new folders are not changes, everything else keeps its rule."""

import os

import pytest

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


@pytest.mark.parametrize("change", ["created", "modified", "restored", "deleted"])
def test_gitignored_files_anywhere_are_output_not_changes(git_history, tmp_path, change):
    names = ["run.log", "src/foragent/_build.py", "tests/acceptance/run.log", "pkg/local-output"]
    files = {".gitignore": "*.log\n/src/foragent/_build.py\n", "pkg/.gitignore": "local-*\n"}
    if change != "created":
        files.update(dict.fromkeys(names, "original\n"))
    git, tree, manifest = _scratch(git_history, tmp_path, files)
    before = order_scratch.stamps(tree, manifest)
    for name in names:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if change == "deleted":
            path.unlink()
        else:
            path.write_text("regenerated\n")
            if change == "restored":
                path.write_text("original\n")
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance", before=before) == (
        {}, [], sorted(names))


@pytest.mark.parametrize("locked", [True, False])
def test_python_scratch_commands_build_their_own_project_environment(tmp_path, locked):
    command = ".venv/bin/python -m pytest tests/acceptance/test_x.py"
    shell = ["/bin/sh", "-c", command]
    assert order_scratch.command_argv(tmp_path, command) == shell
    (tmp_path / "pyproject.toml").write_text("[project]\nname='toy'\nversion='0'\n")
    if locked:
        (tmp_path / "uv.lock").write_text("lock\n")
    assert order_scratch.command_argv(tmp_path, command) == (["uv", "run", "--frozen", "--", *shell]
                                                          if locked else shell)
    assert not (tmp_path / ".venv").exists(), "An exported Git tree has no ignored environment to reuse"

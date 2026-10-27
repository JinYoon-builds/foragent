"""Acceptance test for statusline-repo-slug. Locked by Foragent; do not edit."""

import shutil

import pytest
import yaml

from test_presence_statusline import assert_presence, presence_case  # noqa: F401 (fixture)
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)


def drop_repo_key(case):
    path = case.root / ".foragent.yml"
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    values.pop("repo")
    path.write_text(yaml.safe_dump(values), encoding="utf-8")


def git_config(url):
    return ("[core]\n\trepositoryformatversion = 0\n"
            f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
            '[branch "main"]\n\tremote = origin\n')


@pytest.mark.parametrize("url", ["git@github.com:offline/presence.git",
                                 "https://github.com/offline/presence.git",
                                 "https://github.com/offline/presence"],
                         ids=["ssh", "https-git", "https-bare"])
def test_without_a_repo_key_the_segment_finds_the_slug_from_origin_without_a_child_process(
        presence_case, url):  # noqa: F811
    case = presence_case
    drop_repo_key(case)
    (case.root / ".git" / "config").write_text(git_config(url), encoding="utf-8")
    # The fixture forbids child processes, sockets and sleeps: the slug comes from files.
    assert_presence(case.run())


def test_inside_a_linked_worktree_the_slug_comes_from_the_common_git_dir(presence_case, tmp_path):  # noqa: F811
    case = presence_case
    drop_repo_key(case)
    main_git = tmp_path / "main-checkout" / ".git"
    (main_git / "worktrees" / "issue-1").mkdir(parents=True)
    (main_git / "config").write_text(git_config("git@github.com:offline/presence.git"), encoding="utf-8")
    (main_git / "worktrees" / "issue-1" / "commondir").write_text("../..\n", encoding="utf-8")
    shutil.rmtree(case.root / ".git")
    (case.root / ".git").write_text(f"gitdir: {main_git / 'worktrees' / 'issue-1'}\n", encoding="utf-8")
    assert_presence(case.run())


def test_without_a_repo_key_or_origin_the_segment_shows_zeros_and_never_breaks(presence_case):  # noqa: F811
    case = presence_case
    drop_repo_key(case)
    output = case.run()
    assert output.startswith(b"\x1b[32mClaude\x1b[0m\ncontext 42%"), "Keep the user's statusline"

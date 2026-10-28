"""Launch A5: actionable preflight errors before any setup side effect."""

from pathlib import Path
import shutil
import subprocess

import pytest

from test_init import setup
from test_init_role_agents import init_project
from test_skills_autosync import cli_exit, isolated_home


@pytest.mark.parametrize("problem,fix", [
    ("missing-gh", "brew install gh"),
    ("logged-out", "gh auth login"),
    ("missing-origin", "git remote add origin https://github.com/<owner>/<repo>.git"),
    ("non-github-origin", "git remote set-url origin https://github.com/<owner>/<repo>.git"),
])
def test_github_preflight_exits_with_copyable_fix_before_writes(
    setup, init_project, monkeypatch, capsys, problem, fix,
):
    from foragent import cli

    original_gh, original_git, original_which = setup.run_gh, setup.run_git, shutil.which

    def gh_run(args, input=None):
        if problem == "missing-gh":
            raise FileNotFoundError("gh")
        if problem == "logged-out":
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="Not logged in")
        return original_gh(args, input=input)

    def git_run(args):
        if args == ["remote", "get-url", "origin"]:
            if problem == "missing-origin":
                raise subprocess.CalledProcessError(2, ["git", *args], stderr="No such remote 'origin'")
            if problem == "non-github-origin":
                return "https://gitlab.com/example/project.git"
        return original_git(args)

    monkeypatch.setattr(setup, "run_gh", gh_run)
    monkeypatch.setattr(setup, "run_git", git_run)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw:
                        None if name == "gh" and problem == "missing-gh" else original_which(name, *a, **kw))

    def files(root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(root).parts}

    before = files(setup.root), files(Path.home())
    # No opt-out flags: failure must precede normal push, board and gate setup.
    code = cli_exit(cli.main, ["init", "--repo-root", str(setup.root)])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code != 0, "Invalid GitHub prerequisites must reject init"
    assert (files(setup.root), files(Path.home())) == before, "Preflight must not write project or user setup"
    assert not setup.tracker.labels and not setup.installs, "Preflight must precede GitHub and service mutations"
    assert not any("push" in args or "commit" in args for args in setup.git_calls)
    assert "Traceback" not in output
    assert fix in output, f"Missing copyable recovery command for {problem}: {output!r}"

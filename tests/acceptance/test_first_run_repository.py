"""Acceptance for #1655 (repository steps): `fa init` reads where the folder stands and prints
the next command, creating nothing until the person agrees.

Only git and a fake `gh` are on PATH. The fake logs every call, so the tests can prove that no
repository is created and no install is started on the person's behalf.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from foragent import cli

from conftest import GitHistory, git_command

FAKE_GH = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["auth", "status"]:
    print("Logged in to github.com account human")
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "human", "type": "User"}))
else:
    print("[]")
'''


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key, value in (("GIT_TERMINAL_PROMPT", "0"), ("FORAGENT_UPDATE_CHECK", "off"),
                       ("GIT_AUTHOR_NAME", "Test Human"), ("GIT_AUTHOR_EMAIL", "human@example.com"),
                       ("GIT_COMMITTER_NAME", "Test Human"), ("GIT_COMMITTER_EMAIL", "human@example.com")):
        monkeypatch.setenv(key, value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")

    class Env:
        root = tmp_path

        @staticmethod
        def gh_calls() -> list[list[str]]:
            if not log.is_file():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

    return Env


def run_init(folder: Path, capsys) -> tuple[int, str]:
    try:
        code = cli.main(["init", "--repo-root", str(folder), "--language", "en"])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def assert_offers_new_or_existing_repository(output: str) -> None:
    lowered = output.casefold()
    assert "private" in lowered and "public" in lowered, f"name the private and public choices: {output!r}"
    # An existing GitHub repository is linked with the command the person can copy.
    assert "git remote add origin" in output, f"name how to link an existing repository URL: {output!r}"


def test_plain_folder_prints_git_init_and_the_repository_choices_and_creates_nothing(env, capsys):
    folder = env.root / "plain"
    folder.mkdir()
    (folder / "notes.txt").write_text("my notes\n", encoding="utf-8")
    before = files(folder)

    code, output = run_init(folder, capsys)

    assert code != 0, "init cannot continue without a repository"
    assert "Traceback" not in output
    assert "git init" in output, f"a folder without git must print the next command: {output!r}"
    assert_offers_new_or_existing_repository(output)
    assert files(folder) == before and not (folder / ".git").exists(), "nothing is created before the person agrees"
    assert not any(args[:2] == ["repo", "create"] for args in env.gh_calls())


def test_repository_without_origin_offers_private_public_or_an_existing_url(env, capsys):
    origin, repo = env.root / "origin.git", env.root / "demo"
    git_command(env.root, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, repo).update({"README.md": "# Demo\n", "src/app.py": "x = 1\n"})
    git_command(env.root, "clone", "-q", str(origin), str(repo))
    git(repo, "remote", "remove", "origin")
    head, before = git(repo, "rev-parse", "HEAD"), files(repo)

    code, output = run_init(repo, capsys)

    assert code != 0
    assert "Traceback" not in output
    assert_offers_new_or_existing_repository(output)
    assert not any(args[:2] == ["repo", "create"] for args in env.gh_calls()), "no repository without agreement"
    assert git(repo, "remote") == "", "no remote is linked without agreement"
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert files(repo) == before

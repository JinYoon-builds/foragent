"""Acceptance for #1655 (existing-project survey): `fa adopt survey --json` reads the project's
docs, its open issues and pull requests, and the history that already resolves an open issue,
without changing anything locally or on GitHub.

Only git and a fake `gh` are on PATH; the fake logs every call. The fake ignores `--jq`, so
the survey must read gh's JSON itself.
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

ISSUES = [
    {"number": 12, "title": "Crash on empty input", "state": "OPEN", "body": "It crashes.",
     "labels": [], "url": "https://github.com/example/demo/issues/12"},
    {"number": 13, "title": "crash when the input is empty", "state": "OPEN", "body": "Same here.",
     "labels": [], "url": "https://github.com/example/demo/issues/13"},
    {"number": 14, "title": "Add a --version flag", "state": "OPEN", "body": "Please.",
     "labels": [], "url": "https://github.com/example/demo/issues/14"},
]
PULLS = [
    {"number": 20, "title": "Add usage docs", "state": "OPEN", "body": "Docs.",
     "headRefName": "docs", "url": "https://github.com/example/demo/pull/20"},
]
WRITE_WORDS = {"create", "edit", "close", "comment", "merge", "delete", "reopen", "transfer", "lock"}

FAKE_GH = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
issues, pulls = json.loads(os.environ["FAKE_GH_ISSUES"]), json.loads(os.environ["FAKE_GH_PULLS"])
if args[:2] == ["auth", "status"]:
    print("Logged in to github.com account human")
elif args[:2] == ["issue", "list"]:
    print(json.dumps(issues))
elif args[:2] == ["pr", "list"]:
    print(json.dumps(pulls))
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "example/demo", "isPrivate": True, "description": ""}))
elif args[:1] == ["api"] and any("/pulls" in a for a in args):
    print(json.dumps([{**p, "state": "open"} for p in pulls]))
elif args[:1] == ["api"] and any("/issues" in a for a in args):
    print(json.dumps([{**i, "state": "open"} for i in issues]
                     + [{**p, "state": "open", "pull_request": {"url": p["url"]}} for p in pulls]))
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
    monkeypatch.setenv("FAKE_GH_ISSUES", json.dumps(ISSUES))
    monkeypatch.setenv("FAKE_GH_PULLS", json.dumps(PULLS))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")

    class Env:
        root = tmp_path

        @staticmethod
        def gh_calls() -> list[list[str]]:
            if not log.is_file():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

        @staticmethod
        def without_gh():
            gh.unlink()

    return Env


def existing_project(root: Path) -> tuple[Path, str]:
    origin, repo = root / "origin.git", root / "demo"
    git_command(root, "init", "--bare", "--initial-branch=main", str(origin))
    history = GitHistory(origin, repo)
    history.update({
        "README.md": "# Demo\n\nA command-line tool that counts words.\n",
        "CLAUDE.md": "Keep functions small.\n",
        "src/app.py": "def count(text):\n    return len(text.split())\n",
        "tests/test_app.py": "def test_ok():\n    assert True\n",
    }, message="Add word counter (see #14 for a later flag)")
    history.update({"src/app.py": "def count(text):\n    return len((text or '').split())\n"},
                   message="Fix crash on empty input (fixes #12)")
    history.update({"README.md": "# Demo\n\nCounts words. Usage: demo FILE\n"}, message="Tidy README")
    git_command(root, "clone", "-q", str(origin), str(repo))
    git(repo, "remote", "set-url", "origin", "https://github.com/example/demo.git")
    return repo, git(repo, "log", "--format=%H", "--grep=fixes #12")


def survey(repo: Path, capsys) -> dict:
    try:
        code = cli.main(["adopt", "survey", "--repo-root", str(repo), "--json"])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    return json.loads(captured.out)


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def nodes_for(report: dict, number: int) -> list[dict]:
    return [node for node in walk(report) if isinstance(node, dict) and node.get("number") == number]


def carries(node: dict, sha: str) -> bool:
    return any(isinstance(v, str) and len(v) >= 7 and sha.startswith(v) for v in walk(node))


def test_survey_reads_docs_open_issues_and_prs_without_writing(env, capsys):
    repo, _ = existing_project(env.root)
    head, status = git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")

    report = survey(repo, capsys)

    assert report["kind"] == "existing"
    text = json.dumps(report)
    assert "README.md" in text and "CLAUDE.md" in text, "the survey must say it read README and CLAUDE.md"
    numbers = {node["number"] for node in walk(report) if isinstance(node, dict) and "number" in node}
    assert {12, 13, 14, 20} <= numbers, f"open issues and PRs must be in the survey: {sorted(numbers)}"
    assert env.gh_calls(), "an existing GitHub project is read from GitHub"
    for args in env.gh_calls():
        assert not WRITE_WORDS & set(args), f"survey must only read GitHub: gh {args}"
        methods = [args[i + 1].upper() for i, a in enumerate(args[:-1]) if a in ("-X", "--method")]
        assert all(method == "GET" for method in methods), f"survey must only read GitHub: gh {args}"
        if args[:1] == ["api"] and {"-f", "-F", "--field", "--raw-field", "--input"} & set(args):
            assert methods, f"gh api with fields defaults to POST; survey must only read: gh {args}"
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == status


def test_open_issue_that_history_already_fixes_carries_the_fixing_commit(env, capsys):
    repo, fix_sha = existing_project(env.root)
    other_shas = set(git(repo, "log", "--format=%H").split()) - {fix_sha}

    report = survey(repo, capsys)

    assert any(carries(node, fix_sha) for node in nodes_for(report, 12)), (
        "the open issue a commit already fixes must carry that commit as evidence")
    # Only a closing keyword is evidence: #13 is never named, and #14 is only mentioned.
    for number in (13, 14):
        for sha in {fix_sha, *other_shas}:
            assert not any(carries(node, sha) for node in nodes_for(report, number)), (
                f"#{number} is not resolved by history and must carry no commit evidence")


def test_survey_still_reports_the_local_part_when_gh_is_missing(env, capsys):
    repo, _ = existing_project(env.root)
    env.without_gh()

    report = survey(repo, capsys)

    assert report["kind"] == "existing"
    assert "README.md" in json.dumps(report), "the local part of the survey must not need gh"
